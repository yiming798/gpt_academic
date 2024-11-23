import os
import logging
import asyncio
from pathlib import Path
from typing import List, Optional, Generator, Dict, Union
from datetime import datetime
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import aiohttp

from shared_utils.fastapi_server import validate_path_safety
from toolbox import CatchException, update_ui, get_conf, get_log_folder, update_ui_lastest_msg
from crazy_functions.rag_fns.arxiv_fns.arxiv_splitter import ArxivSplitter, save_fragments_to_file
from crazy_functions.rag_fns.arxiv_fns.section_fragment import SectionFragment as Fragment

from crazy_functions.rag_fns.llama_index_worker import LlamaIndexRagWorker
from crazy_functions.crazy_utils import input_clipping
from crazy_functions.crazy_utils import request_gpt_model_in_new_thread_with_ui_alive

# 全局常量配置
MAX_HISTORY_ROUND = 5  # 最大历史对话轮数
MAX_CONTEXT_TOKEN_LIMIT = 4096  # 上下文最大token数
REMEMBER_PREVIEW = 1000  # 记忆预览长度
VECTOR_STORE_TYPE = "Simple"  # 向量存储类型：Simple或Milvus
MAX_CONCURRENT_PAPERS = 5  # 最大并行处理论文数
MAX_WORKERS = 3  # 最大工作线程数

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class ProcessingTask:
    """论文处理任务数据类"""
    arxiv_id: str
    status: str = "pending"  # pending, processing, completed, failed
    error: Optional[str] = None
    fragments: List[Fragment] = None


class ArxivRagWorker:
    def __init__(self, user_name: str, llm_kwargs: Dict, arxiv_id: str = None):
        self.user_name = user_name
        self.llm_kwargs = llm_kwargs
        self.max_concurrent_papers = MAX_CONCURRENT_PAPERS
        self.arxiv_id = self._normalize_arxiv_id(arxiv_id) if arxiv_id else None

        # Initialize base storage directory
        self.base_dir = Path(get_log_folder(user_name, plugin_name='rag_cache'))

        if self.arxiv_id:
            self.checkpoint_dir = self.base_dir / self.arxiv_id
            self.vector_store_dir = self.checkpoint_dir / "vector_store"
            self.fragment_store_dir = self.checkpoint_dir / "fragments"
        else:
            self.checkpoint_dir = self.base_dir
            self.vector_store_dir = self.base_dir / "vector_store"
            self.fragment_store_dir = self.base_dir / "fragments"

        if os.path.exists(self.vector_store_dir):
            self.loading = True
        else:
            self.loading = False

        # Create necessary directories
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.vector_store_dir.mkdir(parents=True, exist_ok=True)
        self.fragment_store_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Checkpoint directory: {self.checkpoint_dir}")
        logger.info(f"Vector store directory: {self.vector_store_dir}")
        logger.info(f"Fragment store directory: {self.fragment_store_dir}")

        # Initialize processing queue and thread pool
        self.processing_queue = {}
        self.thread_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)

        # Initialize RAG worker
        self.rag_worker = LlamaIndexRagWorker(
            user_name=user_name,
            llm_kwargs=llm_kwargs,
            checkpoint_dir=str(self.vector_store_dir),
            auto_load_checkpoint=True
        )

        # Initialize arxiv splitter
        self.arxiv_splitter = ArxivSplitter(
            root_dir=str(self.checkpoint_dir / "arxiv_cache")
        )
    async def _async_get_fragments(self, arxiv_id: str) -> List[Fragment]:
        """Async helper to get fragments"""
        return await self.arxiv_splitter.process(arxiv_id)

    def _get_fragments_sync(self, arxiv_id: str) -> List[Fragment]:
        """Synchronous wrapper for async fragment retrieval"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._async_get_fragments(arxiv_id))
        finally:
            loop.close()
    def _process_single_fragment(self, fragment: Fragment, index: int) -> None:
        """Process a single paper fragment"""
        try:
            text = (
                f"Paper Title: {fragment.title}\n"
                f"Abstract: {fragment.abstract}\n"
                f"ArXiv ID: {fragment.arxiv_id}\n"
                f"Section: {fragment.current_section}\n"
                f"Section Tree: {fragment.section_tree}\n"
                f"Content: {fragment.content}\n"
                f"Bibliography: {fragment.bibliography}\n"
                f"Type: FRAGMENT"
            )
            logger.info(f"Processing fragment {index} for paper {fragment.arxiv_id}")
            self.rag_worker.add_text_to_vector_store(text)
            logger.info(f"Successfully added fragment {index} to vector store")

        except Exception as e:
            logger.error(f"Error processing fragment {index}: {str(e)}")
            raise

    def _process_fragments(self, fragments: List[Fragment]) -> None:
        """Process paper fragments in parallel using thread pool"""
        if not fragments:
            logger.warning("No fragments to process")
            return

        # First add paper overview
        overview = {
            "title": fragments[0].title,
            "abstract": fragments[0].abstract,
            "arxiv_id": fragments[0].arxiv_id,
            "section_tree": fragments[0].section_tree,
        }

        overview_text = (
            f"Paper Title: {overview['title']}\n"
            f"ArXiv ID: {overview['arxiv_id']}\n"
            f"Abstract: {overview['abstract']}\n"
            f"Section Tree:{overview['section_tree']}\n"
            f"Type: OVERVIEW"
        )

        try:
            # Add overview synchronously
            self.rag_worker.add_text_to_vector_store(overview_text)
            logger.info(f"Added paper overview for {overview['arxiv_id']}")

            # Process fragments in parallel using thread pool
            with ThreadPoolExecutor(max_workers=10) as executor:
                # Submit all fragments for processing
                futures = [
                    executor.submit(self._process_single_fragment, fragment, i)
                    for i, fragment in enumerate(fragments)
                ]

                # Wait for all tasks to complete and handle any exceptions
                for future in futures:
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Error processing fragment: {str(e)}")

            logger.info(f"Processed {len(fragments)} fragments successfully")

            # Save to local file for debugging
            save_fragments_to_file(
                fragments,
                str(self.fragment_store_dir / f"{overview['arxiv_id']}_fragments.json")
            )

        except Exception as e:
            logger.error(f"Error processing fragments: {str(e)}")
            raise

    def process_paper(self, arxiv_id: str) -> bool:
        """Process paper main function - mixed sync/async version"""
        try:
            arxiv_id = self._normalize_arxiv_id(arxiv_id)
            logger.info(f"Starting to process paper: {arxiv_id}")

            paper_path = self.checkpoint_dir / f"{arxiv_id}.processed"

            if paper_path.exists():
                logger.info(f"Paper {arxiv_id} already processed")
                return True

            # Create processing task
            task = ProcessingTask(arxiv_id=arxiv_id)
            self.processing_queue[arxiv_id] = task
            task.status = "processing"

            # Download and split paper using the sync wrapper
            fragments = self._get_fragments_sync(arxiv_id)

            if not fragments:
                raise ValueError(f"No fragments extracted from paper {arxiv_id}")

            logger.info(f"Got {len(fragments)} fragments from paper {arxiv_id}")

            # Process fragments
            self._process_fragments(fragments)

            # Mark as completed
            paper_path.touch()
            task.status = "completed"
            task.fragments = fragments

            logger.info(f"Successfully processed paper {arxiv_id}")
            return True

        except Exception as e:
            logger.error(f"Error processing paper {arxiv_id}: {str(e)}")
            if arxiv_id in self.processing_queue:
                self.processing_queue[arxiv_id].status = "failed"
                self.processing_queue[arxiv_id].error = str(e)
            return False
    def wait_for_paper(self, arxiv_id: str, timeout: float = 300.0) -> bool:
        """Wait for paper processing to complete - synchronous version"""
        try:
            start_time = datetime.now()
            while True:
                task = self.processing_queue.get(arxiv_id)
                if not task:
                    return False

                if task.status == "completed":
                    return True

                if task.status == "failed":
                    return False

                # Check timeout
                if (datetime.now() - start_time).total_seconds() > timeout:
                    logger.error(f"Processing paper {arxiv_id} timed out")
                    return False

                time.sleep(0.1)
        except Exception as e:
            logger.error(f"Error waiting for paper {arxiv_id}: {str(e)}")
            return False

    def _normalize_arxiv_id(self, input_str: str) -> str:
        """Normalize ArXiv ID"""
        if 'arxiv.org/' in input_str.lower():
            if '/pdf/' in input_str:
                arxiv_id = input_str.split('/pdf/')[-1]
            else:
                arxiv_id = input_str.split('/abs/')[-1]
            return arxiv_id.split('v')[0].strip()
        return input_str.split('v')[0].strip()


    def retrieve_and_generate(self, query: str) -> str:
        """检索相关内容并生成提示词"""
        try:
            nodes = self.rag_worker.retrieve_from_store_with_query(query)
            return self.rag_worker.build_prompt(query=query, nodes=nodes)
        except Exception as e:
            logger.error(f"Error in retrieve and generate: {str(e)}")
            return ""

    def remember_qa(self, question: str, answer: str) -> None:
        """记忆问答对"""
        try:
            self.rag_worker.remember_qa(question, answer)
        except Exception as e:
            logger.error(f"Error remembering QA: {str(e)}")

    async def auto_analyze_paper(self, chatbot: List, history: List, system_prompt: str) -> None:
        """自动分析论文的关键问题"""
        key_questions = [
            "What is the main research question or problem addressed in this paper?",
            "What methods or approaches did the authors use to investigate the problem?",
            "What are the key findings or results presented in the paper?",
            "How do the findings of this paper contribute to the broader field or topic of study?",
            "What are the limitations of this study, and what future research directions do the authors suggest?"
        ]

        results = []
        for question in key_questions:
            try:
                prompt = self.retrieve_and_generate(question)
                if prompt:
                    response = await request_gpt_model_in_new_thread_with_ui_alive(
                        inputs=prompt,
                        inputs_show_user=question,
                        llm_kwargs=self.llm_kwargs,
                        chatbot=chatbot,
                        history=history,
                        sys_prompt=system_prompt
                    )
                    results.append(f"Q: {question}\nA: {response}\n")
                    self.remember_qa(question, response)
            except Exception as e:
                logger.error(f"Error in auto analysis: {str(e)}")

        # 合并所有结果
        summary = "\n\n".join(results)
        chatbot[-1] = (chatbot[-1][0], f"论文已成功加载并完成初步分析：\n\n{summary}\n\n您现在可以继续提问更多细节。")

@CatchException
def Arxiv论文对话(txt: str, llm_kwargs: Dict, plugin_kwargs: Dict, chatbot: List,
                  history: List, system_prompt: str, web_port: str) -> Generator:
    """
    Arxiv论文对话主函数
    Args:
        txt: arxiv ID/URL
        llm_kwargs: LLM配置参数
        plugin_kwargs: 插件配置参数，包含 advanced_arg 字段作为用户询问指令
        chatbot: 对话历史
        history: 聊天历史
        system_prompt: 系统提示词
        web_port: Web端口
    """
    # 初始化时，提示用户需要 arxiv ID/URL
    if len(history) == 0 and not txt.lower().strip().startswith(('https://arxiv.org', 'arxiv.org', '0','1', '2')):
        chatbot.append((txt, "请先提供Arxiv论文链接或ID。"))
        yield from update_ui(chatbot=chatbot, history=history)
        return

    user_name = chatbot.get_user()
    worker = ArxivRagWorker(user_name, llm_kwargs, arxiv_id=txt)

    # 处理新论文的情况
    if txt.lower().strip().startswith(('https://arxiv.org', 'arxiv.org', '0', '1', '2')) and not worker.loading:
        chatbot.append((txt, "正在处理论文，请稍等..."))
        yield from update_ui(chatbot=chatbot, history=history)

        success = worker.process_paper(txt)
        if success:
            arxiv_id = worker._normalize_arxiv_id(txt)
            success = worker.wait_for_paper(arxiv_id)

        if not success:
            chatbot[-1] = (txt, "论文处理失败，请检查论文ID是否正确或稍后重试。")
            yield from update_ui(chatbot=chatbot, history=history)
            return

        yield from update_ui(chatbot=chatbot, history=history)
        return

    # 处理用户询问的情况
    # 获取用户询问指令
    user_query = plugin_kwargs.get("advanced_arg", "What is the main research question or problem addressed in this paper?")
    user_query = "What is the main research question or problem addressed in this paper about graph attention network?"
    # if not user_query:
    #     chatbot.append((txt, "请提供您的问题。"))
    #     yield from update_ui(chatbot=chatbot, history=history)
    #     return

    # 处理历史对话长度
    if len(history) > MAX_HISTORY_ROUND * 2:
        history = history[-(MAX_HISTORY_ROUND * 2):]

    # 处理询问指令
    query_clip, history, flags = input_clipping(
        user_query,
        history,
        max_token_limit=MAX_CONTEXT_TOKEN_LIMIT,
        return_clip_flags=True
    )

    if flags["original_input_len"] != flags["clipped_input_len"]:
        yield from update_ui_lastest_msg('检测到长输入，正在处理...', chatbot, history, delay=0)
        if len(user_query) > REMEMBER_PREVIEW:
            HALF = REMEMBER_PREVIEW // 2
            query_to_remember = user_query[:HALF] + f" ...\n...(省略{len(user_query) - REMEMBER_PREVIEW}字)...\n... " + user_query[-HALF:]
        else:
            query_to_remember = query_clip
    else:
        query_to_remember = query_clip

    chatbot.append((user_query, "正在思考中..."))
    yield from update_ui(chatbot=chatbot, history=history)

    # 生成提示词
    prompt = worker.retrieve_and_generate(query_clip)
    if not prompt:
        chatbot[-1] = (user_query, "抱歉，处理您的问题时出现错误，请重试。")
        yield from update_ui(chatbot=chatbot, history=history)
        return

    # 获取回答
    response = yield from request_gpt_model_in_new_thread_with_ui_alive(
        inputs=prompt,
        inputs_show_user=query_clip,
        llm_kwargs=llm_kwargs,
        chatbot=chatbot,
        history=history,
        sys_prompt=system_prompt
    )

    # 记忆问答对
    worker.remember_qa(query_to_remember, response)
    history.extend([user_query, response])

    yield from update_ui(chatbot=chatbot, history=history)

if __name__ == "__main__":
    # 测试代码
    llm_kwargs = {
        'api_key': os.getenv("one_api_key"),
        'client_ip': '127.0.0.1',
        'embed_model': 'text-embedding-3-small',
        'llm_model': 'one-api-Qwen2.5-72B-Instruct',
        'max_length': 4096,
        'most_recent_uploaded': None,
        'temperature': 1,
        'top_p': 1
    }
    plugin_kwargs = {}
    chatbot = []
    history = []
    system_prompt = "You are a helpful assistant."
    web_port = "8080"

    # 测试论文导入
    arxiv_url = "https://arxiv.org/abs/2312.12345"
    for response in Arxiv论文对话(
            arxiv_url, llm_kwargs, plugin_kwargs,
            chatbot, history, system_prompt, web_port
    ):
        print(response)

    # 测试问答
    question = "这篇论文的主要贡献是什么？"
    for response in Arxiv论文对话(
            question, llm_kwargs, plugin_kwargs,
            chatbot, history, system_prompt, web_port
    ):
        print(response)