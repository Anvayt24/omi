import datetime
import uuid
import asyncio
from typing import List, Optional, Tuple, AsyncGenerator

from langchain.callbacks.base import BaseCallbackHandler
from langchain_core.messages import SystemMessage, AIMessage, HumanMessage, ToolMessage
from utils.llm.clients import should_use_tools_for_question
from langchain_openai import ChatOpenAI
from langchain.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END
from langgraph.graph import START, StateGraph
from typing_extensions import TypedDict, Literal

# import os
# os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '../../' + os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
import database.conversations as conversations_db
import database.users as users_db
from database.redis_db import get_filter_category_items
from database.vector_db import query_vectors_by_metadata
import database.notifications as notification_db
from models.app import App
from models.chat import ChatSession, Message
from models.conversation import Conversation
from models.other import Person
from utils.llm.chat import (
    answer_omi_question,
    answer_omi_question_stream,
    requires_context,
    answer_simple_message,
    answer_simple_message_stream,
    retrieve_context_dates_by_question,
    qa_rag,
    qa_rag_stream,
    retrieve_is_an_omi_question,
    retrieve_is_file_question,
    select_structured_filters,
    extract_question_from_conversation,
)
from utils.llm.persona import answer_persona_question_stream
from utils.other.chat_file import FileChatTool
from utils.other.endpoints import timeit
from utils.app_integrations import get_github_docs_content
from utils.llm.clients import generate_embedding
from utils.tools.custom_tools import get_memories_tool, get_conversations_tool, get_action_items_tool
from pydantic import BaseModel

model = ChatOpenAI(model="gpt-4o-mini")
llm_medium_stream = ChatOpenAI(model='gpt-4o', streaming=True)


class StructuredFilters(TypedDict):
    topics: List[str]
    people: List[str]
    entities: List[str]
    dates: List[datetime.date]


class DateRangeFilters(TypedDict):
    start: datetime.datetime
    end: datetime.datetime


class AsyncStreamingCallback(BaseCallbackHandler):
    def __init__(self):
        self.queue = asyncio.Queue()

    async def put_data(self, text):
        await self.queue.put(f"data: {text}")

    async def put_thought(self, text):
        await self.queue.put(f"think: {text}")

    def put_thought_nowait(self, text):
        self.queue.put_nowait(f"think: {text}")

    async def end(self):
        await self.queue.put(None)

    async def on_llm_new_token(self, token: str, **kwargs) -> None:
        await self.put_data(token)

    async def on_llm_end(self, response, **kwargs) -> None:
        await self.end()

    async def on_llm_error(self, error: Exception, **kwargs) -> None:
        print(f"Error on LLM {error}")
        await self.end()

    def put_data_nowait(self, text):
        self.queue.put_nowait(f"data: {text}")

    def end_nowait(self):
        self.queue.put_nowait(None)


class GraphState(TypedDict):
    uid: str
    messages: List[Message]
    plugin_selected: Optional[App]
    tz: str
    cited: Optional[bool] = False

    streaming: Optional[bool] = False
    callback: Optional[AsyncStreamingCallback] = None

    filters: Optional[StructuredFilters]
    date_filters: Optional[DateRangeFilters]

    memories_found: Optional[List[Conversation]]

    parsed_question: Optional[str]
    answer: Optional[str]
    ask_for_nps: Optional[bool]

    chat_session: Optional[ChatSession]


def determine_conversation(state: GraphState):
    print("determine_conversation")
    question = extract_question_from_conversation(state.get("messages", []))
    print("determine_conversation parsed question:", question)

    # # stream
    # if state.get('streaming', False):
    #     state['callback'].put_thought_nowait(question)

    return {"parsed_question": question}


def determine_conversation_type(
    state: GraphState,
) -> Literal[
    "no_context_conversation",
    "context_dependent_conversation",
    "omi_question",
    "file_chat_question",
    "persona_question",
]:
    messages = state.get("messages", [])
    if len(messages) > 0 and len(messages[-1].files_id) > 0:
        return "file_chat_question"

    # persona
    app: App = state.get("plugin_selected")
    if app and app.is_a_persona():
        # file
        question = state.get("parsed_question", "")
        is_file_question = retrieve_is_file_question(question)
        if is_file_question:
            return "file_chat_question"

        return "persona_question"

    # chat
    # no context
    question = state.get("parsed_question", "")
    if not question or len(question) == 0:
        return "no_context_conversation"

    # determine the follow-up question is chatting with files or not
    is_file_question = retrieve_is_file_question(question)
    if is_file_question:
        return "file_chat_question"

    # Use LLM to intelligently decide if tools are needed (more reliable than keywords)
    try:
        tool_decision = should_use_tools_for_question(question)

        if tool_decision.needs_tools:
            print(
                f"LLM decision: Tools needed - {tool_decision.reasoning} (suggested: {tool_decision.suggested_tool_type})"
            )
            print(f"Question needs tools, routing to context_dependent_conversation: '{question}'")
            return "context_dependent_conversation"
        else:
            print(f"LLM decision: No tools needed - {tool_decision.reasoning}")
    except Exception as e:
        print(f"Tool decision LLM failed: {e}, falling back to context check")

    is_omi_question = retrieve_is_an_omi_question(question)
    if is_omi_question:
        return "omi_question"

    requires = requires_context(question)
    if requires:
        return "context_dependent_conversation"
    return "no_context_conversation"


def no_context_conversation(state: GraphState):

    # Normal flow
    streaming = state.get("streaming")
    if streaming:
        answer: str = answer_simple_message_stream(
            state.get("uid"), state.get("messages"), state.get("plugin_selected"), callbacks=[state.get('callback')]
        )
        return {"answer": answer, "ask_for_nps": False}

    # no streaming
    answer: str = answer_simple_message(
        state.get("uid"),
        state.get("messages"),
        state.get("plugin_selected"),
    )
    return {"answer": answer, "ask_for_nps": False}


def omi_question(state: GraphState):

    context: dict = get_github_docs_content()
    context_str = 'Documentation:\n\n'.join([f'{k}:\n {v}' for k, v in context.items()])

    # streaming
    streaming = state.get("streaming")
    if streaming:
        # state['callback'].put_thought_nowait("Reasoning")
        answer: str = answer_omi_question_stream(
            state.get("messages", []), context_str, callbacks=[state.get('callback')]
        )
        return {'answer': answer, 'ask_for_nps': True}

    # no streaming
    answer = answer_omi_question(state.get("messages", []), context_str)
    return {'answer': answer, 'ask_for_nps': True}


def persona_question(state: GraphState):

    # streaming
    streaming = state.get("streaming")
    if streaming:
        # state['callback'].put_thought_nowait("Reasoning")
        answer: str = answer_persona_question_stream(
            state.get("plugin_selected"), state.get("messages", []), callbacks=[state.get('callback')]
        )
        return {'answer': answer, 'ask_for_nps': True}

    # no streaming
    return {'answer': "Oops", 'ask_for_nps': True}


def context_dependent_conversation_v1(state: GraphState):
    question = extract_question_from_conversation(state.get("messages", []))
    print("context_dependent_conversation parsed question:", question)
    return {"parsed_question": question}


def context_dependent_conversation(state: GraphState):
    """Restored original function - just passes state to continue RAG pipeline"""
    return state


# !! include a question extractor? node?


def retrieve_topics_filters(state: GraphState):
    print("retrieve_topics_filters")
    filters = {
        "people": get_filter_category_items(state.get("uid"), "people", limit=1000),
        "topics": get_filter_category_items(state.get("uid"), "topics", limit=1000),
        "entities": get_filter_category_items(state.get("uid"), "entities", limit=1000),
        # 'dates': get_filter_category_items(state.get('uid'), 'dates'),
    }
    result = select_structured_filters(state.get("parsed_question", ""), filters)
    filters = {
        "topics": result.get("topics", []),
        "people": result.get("people", []),
        "entities": result.get("entities", []),
        # 'dates': result.get('dates', []),
    }
    print("retrieve_topics_filters filters", filters)
    return {"filters": filters}


def retrieve_date_filters(state: GraphState):
    print('retrieve_date_filters')
    # TODO: if this makes vector search fail further, query firestore instead
    dates_range = retrieve_context_dates_by_question(state.get("parsed_question", ""), state.get("tz", "UTC"))
    print('retrieve_date_filters dates_range:', dates_range)
    if dates_range and len(dates_range) >= 2:
        return {"date_filters": {"start": dates_range[0], "end": dates_range[1]}}
    return {"date_filters": {}}


def query_vectors(state: GraphState):
    print("query_vectors")

    # # stream
    # if state.get('streaming', False):
    #     state['callback'].put_thought_nowait("Searching through your memories")

    date_filters = state.get("date_filters")
    uid = state.get("uid")
    parsed_question = state.get("parsed_question", "")

    # 🚀 HYBRID SEARCH: Combine semantic similarity with metadata filtering
    if parsed_question and parsed_question.strip():
        # Use real semantic embeddings for better relevance
        try:
            vector = generate_embedding(parsed_question)
            # Hybrid search for semantic + metadata
            print("query_vectors vector (semantic):", vector[:5])
        except Exception as e:
            print(f"ERROR generating embedding: {e}")
            print("Falling back to metadata-only search")
            vector = [1] * 3072
            print("query_vectors vector (fallback metadata-only):", vector[:5])
    else:
        # Fallback to metadata-only search for empty/generic queries
        vector = [1] * 3072
        print("query_vectors vector (metadata-only):", vector[:5])

    # TODO: enable it when the in-accurate topic filter get fixed
    is_topic_filter_enabled = date_filters.get("start") is None

    memories_id = query_vectors_by_metadata(
        uid,
        vector,
        dates_filter=[date_filters.get("start"), date_filters.get("end")],
        people=state.get("filters", {}).get("people", []) if is_topic_filter_enabled else [],
        topics=state.get("filters", {}).get("topics", []) if is_topic_filter_enabled else [],
        entities=state.get("filters", {}).get("entities", []) if is_topic_filter_enabled else [],
        dates=state.get("filters", {}).get("dates", []),
        limit=100,
    )
    memories = conversations_db.get_conversations_by_id(uid, memories_id)

    # stream
    # if state.get('streaming', False):
    #    if len(memories) == 0:
    #        msg = "No relevant memories found"
    #    else:
    #        msg = f"Found {len(memories)} relevant memories"
    #    state['callback'].put_thought_nowait(msg)

    # print(memories_id)
    return {"memories_found": memories}


def file_chat_question(state: GraphState):

    fc_tool = FileChatTool()

    uid = state.get("uid", "")
    question = state.get("parsed_question", "")

    messages = state.get("messages", [])
    last_message = messages[-1] if messages else None

    file_ids = []
    chat_session = state.get("chat_session")
    if chat_session:
        if last_message:
            if len(last_message.files_id) > 0:
                file_ids = last_message.files_id
            else:
                # if user asked about file but not attach new file, will get all file in session
                file_ids = chat_session.file_ids
    else:
        file_ids = fc_tool.get_files()

    streaming = state.get("streaming")
    if streaming:
        answer = fc_tool.process_chat_with_file_stream(uid, question, file_ids, callback=state.get('callback'))
        return {'answer': answer, 'ask_for_nps': True}

    answer = fc_tool.process_chat_with_file(uid, question, file_ids)
    return {'answer': answer, 'ask_for_nps': True}


# === UNIFIED AGENT WITH TOOLS ===


def create_user_tools(uid: str):
    """Create tools with uid pre-populated using closures - eliminates parameter confusion"""

    @tool
    def get_user_memories(date_query: str = None) -> dict:
        """Retrieve user memories filtered by date.

        Args:
            date_query: Natural language date like 'yesterday', 'september 6th', 'today'
        """
        return get_memories_tool(uid, date_query)

    @tool
    def get_user_conversations(date_query: str = None) -> dict:
        """Retrieve user conversations filtered by date.

        Args:
            date_query: Natural language date like 'yesterday', 'september 6th', 'today'
        """
        return get_conversations_tool(uid, date_query)

    @tool
    def get_user_action_items(date_query: str = None) -> dict:
        """Retrieve user action items filtered by date.

        Args:
            date_query: Natural language date like 'yesterday', 'september 6th', 'today'
        """
        return get_action_items_tool(uid, date_query)

    return [get_user_memories, get_user_conversations, get_user_action_items]


def unified_agent_qa_handler(state: GraphState):
    """UNIFIED node that handles both RAG and tools in a single streaming response"""

    uid = state.get("uid")
    memories = state.get("memories_found", [])
    question = state.get("parsed_question", "")
    streaming = state.get("streaming")
    callback = state.get("callback")

    # Build RAG context (existing logic from qa_handler)
    all_person_ids = []
    for m in memories:
        segments = m.get('transcript_segments', [])
        all_person_ids.extend([s.get('person_id') for s in segments if s.get('person_id')])

    people = []
    if all_person_ids:
        people_data = users_db.get_people_by_ids(uid, list(set(all_person_ids)))
        people = [Person(**p) for p in people_data]

    # Use memory context
    combined_context = Conversation.conversations_to_string(memories, False, people=people)

    try:
        # Create user-specific tools (uid pre-populated)
        tools = create_user_tools(uid)

        # Create LLM with tools
        if streaming:
            llm_with_tools = ChatOpenAI(model="gpt-4o", streaming=True).bind_tools(tools)
        else:
            llm_with_tools = ChatOpenAI(model="gpt-4o").bind_tools(tools)

        # Enhanced system prompt that combines RAG context with tool availability
        system_prompt = f"""You are an AI assistant with access to the user's personal data through specialized tools.

CURRENT CONTEXT (from vector search):
{combined_context[:2000] if combined_context else "No relevant memories found in vector search."}

AVAILABLE TOOLS:
- get_user_memories(date_query): Get user's memories. Use date_query for specific dates like "September 1st", "yesterday", or leave empty for all memories.
- get_user_conversations(date_query): Get user's conversations. Use date_query for specific dates or leave empty for all.
- get_user_action_items(date_query): Get user's action items/tasks. Use date_query for specific dates or leave empty for all.

TOOL USAGE RULES:
1. If user asks for "memories from September 6th" → use get_user_memories(date_query="September 6th")
2. If user asks for "all my action items" → use get_user_action_items() with no date_query
3. If user asks for "conversations from yesterday" → use get_user_conversations(date_query="yesterday")
4. ALWAYS use tools when user asks for memories, conversations, or action items
5. Use the context above only for general questions

IMPORTANT:
- date_query parameter is OPTIONAL - omit it if no specific date is mentioned
- When user specifies dates, always pass them in the date_query parameter
- Provide natural, helpful responses combining tool results with context

User's timezone: {state.get('tz', 'UTC')}"""

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=question)]

        # SIMPLIFIED APPROACH: Use invoke for tool calls, then stream the final response
        # This avoids complex streaming + tool call coordination issues

        if streaming and callback:
            # First, get the complete response with tool calls (non-streaming)
            print("Using intelligent agent mode with tools")
            print("Getting response with tool calls...")
            full_response = llm_with_tools.invoke(messages)

            # Debug: Log the full response structure
            print(f"Response type: {type(full_response)}")
            print(f"Has content: {hasattr(full_response, 'content')}")
            print(f"Has tool_calls: {hasattr(full_response, 'tool_calls')}")

            if hasattr(full_response, 'content'):
                content_length = len(full_response.content) if full_response.content else 0
                print(f"Content: '{full_response.content[:100]}...' (length: {content_length})")
            if hasattr(full_response, 'tool_calls'):
                tool_calls_count = len(full_response.tool_calls) if full_response.tool_calls else 0
                print(f"Tool calls count: {tool_calls_count}")
                for i, tc in enumerate(full_response.tool_calls or []):
                    print(f"Tool call {i+1}: {tc.get('name', 'UNKNOWN')} with args: {tc.get('args', {})}")

            # Check if this response contains tool calls that need execution
            if hasattr(full_response, 'tool_calls') and full_response.tool_calls:
                print(f"Found {len(full_response.tool_calls)} tool calls to execute")
                tool_messages = []

                # Execute each tool call
                for i, tool_call in enumerate(full_response.tool_calls):
                    print(f"Executing tool call {i+1}: {tool_call.get('name', 'UNKNOWN')}")

                    try:
                        tool_name = tool_call['name']
                        tool_args = tool_call['args']
                        tool_call_id = tool_call.get('id', f"call_{i}")

                        print(f"Calling {tool_name} with args: {tool_args}")

                        # Find and execute the tool
                        tool_result = None
                        for tool in tools:
                            if tool.name == tool_name:
                                tool_result = tool.invoke(tool_args)
                                result_preview = (
                                    str(tool_result)[:200] + "..." if len(str(tool_result)) > 200 else str(tool_result)
                                )
                                print(f"Tool {tool_name} result: {result_preview}")
                                break

                        if tool_result is not None:
                            tool_messages.append(ToolMessage(content=str(tool_result), tool_call_id=tool_call_id))
                        else:
                            print(f"No matching tool found for: {tool_name}")
                            tool_messages.append(
                                ToolMessage(
                                    content=f"Error: No tool found with name {tool_name}", tool_call_id=tool_call_id
                                )
                            )

                    except Exception as e:
                        print(f"Tool execution error for {tool_call.get('name', 'UNKNOWN')}: {e}")
                        tool_messages.append(
                            ToolMessage(content=f"Error: {str(e)}", tool_call_id=tool_call.get('id', f"error_{i}"))
                        )

                # Now get the final response with tool results
                print(f"Getting final response with {len(tool_messages)} tool results...")
                final_messages = messages + [full_response] + tool_messages

                final_response = llm_with_tools.invoke(final_messages)
                print(f"Final response type: {type(final_response)}")
                print(f"Final response has content: {hasattr(final_response, 'content')}")

                if hasattr(final_response, 'content') and final_response.content:
                    final_answer = final_response.content
                    print(f"Final answer length: {len(final_answer)} chars")
                    print(f"Final answer preview: {final_answer[:100]}...")
                else:
                    print(f"Final response has no content: {final_response}")
                    final_answer = "I apologize, but I couldn't process the tool results properly."

            elif hasattr(full_response, 'content') and full_response.content:
                # Direct response without tool calls
                final_answer = full_response.content
                print(f"Direct response (no tools): {len(final_answer)} chars")
                print(f"Direct response preview: {final_answer[:100]}...")
            else:
                print("Response has no content and no tool calls")
                print(f"Full response debug: {full_response}")
                final_answer = "I apologize, but I couldn't generate a proper response."

            # Stream the final answer in chunks
            if final_answer and final_answer.strip():
                print(f"Streaming answer: '{final_answer[:50]}...'")
                words = final_answer.split()
                current_chunk = ""

                for word in words:
                    current_chunk += word + " "
                    # Stream every 3-4 words or when chunk gets long enough
                    if len(current_chunk.split()) >= 3 or len(current_chunk) > 50:
                        callback.put_data_nowait(current_chunk)
                        current_chunk = ""

                # Stream any remaining content
                if current_chunk.strip():
                    callback.put_data_nowait(current_chunk)

                # Signal end of streaming
                callback.put_data_nowait(None)
                enhanced_response = final_answer
            else:
                print("No final answer to stream")
                enhanced_response = "I apologize, but I couldn't generate a proper response."
                callback.put_data_nowait(enhanced_response)
                # Signal end of streaming
                callback.put_data_nowait(None)

        else:
            # Non-streaming mode - let LangGraph handle tool calls automatically
            response = llm_with_tools.invoke(messages)
            enhanced_response = response.content if hasattr(response, 'content') else str(response)

        print("Unified agent response generated successfully")
        return {
            "answer": enhanced_response,
            "ask_for_nps": True,
        }

    except Exception as e:
        print(f"Agent mode failed, falling back to RAG: {e}")
        # Graceful fallback to standard RAG
        if streaming:
            response = qa_rag_stream(
                uid,
                question,
                combined_context,
                state.get("plugin_selected"),
                cited=state.get("cited"),
                messages=state.get("messages"),
                tz=state.get("tz"),
                callbacks=[callback],
            )
        else:
            response = qa_rag(
                uid,
                question,
                combined_context,
                state.get("plugin_selected"),
                cited=state.get("cited"),
                messages=state.get("messages"),
                tz=state.get("tz"),
            )

        return {"answer": response, "ask_for_nps": True}


workflow = StateGraph(GraphState)

workflow.add_edge(START, "determine_conversation")

workflow.add_node("determine_conversation", determine_conversation)

workflow.add_conditional_edges("determine_conversation", determine_conversation_type)


workflow.add_node("no_context_conversation", no_context_conversation)
workflow.add_node("omi_question", omi_question)
workflow.add_node("context_dependent_conversation", context_dependent_conversation)
workflow.add_node("file_chat_question", file_chat_question)
workflow.add_node("persona_question", persona_question)

workflow.add_edge("no_context_conversation", END)
workflow.add_edge("omi_question", END)
workflow.add_edge("persona_question", END)
workflow.add_edge("file_chat_question", END)
workflow.add_edge("context_dependent_conversation", "retrieve_topics_filters")
workflow.add_edge("context_dependent_conversation", "retrieve_date_filters")

workflow.add_node("retrieve_topics_filters", retrieve_topics_filters)
workflow.add_node("retrieve_date_filters", retrieve_date_filters)

workflow.add_edge("retrieve_topics_filters", "query_vectors")
workflow.add_edge("retrieve_date_filters", "query_vectors")

workflow.add_node("query_vectors", query_vectors)

workflow.add_edge("query_vectors", "unified_agent_qa_handler")

workflow.add_node("unified_agent_qa_handler", unified_agent_qa_handler)

workflow.add_edge("unified_agent_qa_handler", END)

checkpointer = MemorySaver()
graph = workflow.compile(checkpointer=checkpointer)

graph_stream = workflow.compile()


@timeit
def execute_graph_chat(
    uid: str,
    messages: List[Message],
    app: Optional[App] = None,
    cited: Optional[bool] = False,
) -> Tuple[str, bool, List[Conversation]]:
    print('execute_graph_chat app    :', app.id if app else '<none>')
    tz = notification_db.get_user_time_zone(uid)
    result = graph.invoke(
        {
            "uid": uid,
            "tz": tz,
            "cited": cited,
            "messages": messages,
            "plugin_selected": app,
        },
        {"configurable": {"thread_id": str(uuid.uuid4())}},
    )
    return result.get("answer"), result.get('ask_for_nps', False), result.get("memories_found", [])


async def execute_graph_chat_stream(
    uid: str,
    messages: List[Message],
    app: Optional[App] = None,
    cited: Optional[bool] = False,
    callback_data: dict = {},
    chat_session: Optional[ChatSession] = None,
) -> AsyncGenerator[str, None]:
    print('execute_graph_chat_stream app: ', app.id if app else '<none>')
    tz = notification_db.get_user_time_zone(uid)
    callback = AsyncStreamingCallback()

    task = asyncio.create_task(
        graph_stream.ainvoke(
            {
                "uid": uid,
                "tz": tz,
                "cited": cited,
                "messages": messages,
                "plugin_selected": app,
                "streaming": True,
                "callback": callback,
                "chat_session": chat_session,
            },
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
    )

    while True:
        try:
            chunk = await callback.queue.get()
            if chunk:
                yield chunk
            else:
                break
        except asyncio.CancelledError:
            break
    await task
    result = task.result()
    callback_data['answer'] = result.get("answer")
    callback_data['memories_found'] = result.get("memories_found", [])
    callback_data['ask_for_nps'] = result.get('ask_for_nps', False)

    yield None
    return


async def execute_persona_chat_stream(
    uid: str,
    messages: List[Message],
    app: App,
    cited: Optional[bool] = False,
    callback_data: dict = None,
    chat_session: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Handle streaming chat responses for persona-type apps"""

    system_prompt = app.persona_prompt
    formatted_messages = [SystemMessage(content=system_prompt)]

    for msg in messages:
        if msg.sender == "ai":
            formatted_messages.append(AIMessage(content=msg.text))
        else:
            formatted_messages.append(HumanMessage(content=msg.text))

    full_response = []
    callback = AsyncStreamingCallback()

    try:
        task = asyncio.create_task(llm_medium_stream.agenerate(messages=[formatted_messages], callbacks=[callback]))

        while True:
            try:
                chunk = await callback.queue.get()
                if chunk:
                    token = chunk.replace("data: ", "")
                    full_response.append(token)
                    yield chunk
                else:
                    break
            except asyncio.CancelledError:
                break

        await task

        if callback_data is not None:
            callback_data['answer'] = ''.join(full_response)
            callback_data['memories_found'] = []
            callback_data['ask_for_nps'] = False

        yield None
        return

    except Exception as e:
        print(f"Error in execute_persona_chat_stream: {e}")
        if callback_data is not None:
            callback_data['error'] = str(e)
        yield None
        return
