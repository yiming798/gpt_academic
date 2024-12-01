from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type, TypeVar, Generic, Union
from dataclasses import dataclass
from enum import Enum, auto
import logging
from datetime import datetime
from crazy_functions.rag_fns.arxiv_fns.section_fragment import SectionFragment

# 设置日志
logger = logging.getLogger(__name__)


# 自定义异常类定义
class FoldingError(Exception):
    """折叠相关的自定义异常基类"""
    pass


class FormattingError(FoldingError):
    """格式化过程中的错误"""
    pass


class MetadataError(FoldingError):
    """元数据相关的错误"""
    pass


class ValidationError(FoldingError):
    """验证错误"""
    pass


class FoldingStyle(Enum):
    """折叠样式枚举"""
    SIMPLE = auto()  # 简单折叠
    DETAILED = auto()  # 详细折叠（带有额外信息）
    NESTED = auto()  # 嵌套折叠


@dataclass
class FoldingOptions:
    """折叠选项配置"""
    style: FoldingStyle = FoldingStyle.DETAILED
    code_language: Optional[str] = None  # 代码块的语言
    show_timestamp: bool = False  # 是否显示时间戳
    indent_level: int = 0  # 缩进级别
    custom_css: Optional[str] = None  # 自定义CSS类


T = TypeVar('T')  # 用于泛型类型


class BaseMetadata(ABC):
    """元数据基类"""

    @abstractmethod
    def validate(self) -> bool:
        """验证元数据的有效性"""
        pass

    def _validate_non_empty_str(self, value: Optional[str]) -> bool:
        """验证字符串非空"""
        return bool(value and value.strip())


@dataclass
class FileMetadata(BaseMetadata):
    """文件元数据"""
    rel_path: str
    size: float
    last_modified: Optional[datetime] = None
    mime_type: Optional[str] = None
    encoding: str = 'utf-8'

    def validate(self) -> bool:
        """验证文件元数据的有效性"""
        try:
            if not self._validate_non_empty_str(self.rel_path):
                return False
            if self.size < 0:
                return False
            return True
        except Exception as e:
            logger.error(f"File metadata validation error: {str(e)}")
            return False




class ContentFormatter(ABC, Generic[T]):
    """内容格式化抽象基类

    支持泛型类型参数，可以指定具体的元数据类型。
    """

    @abstractmethod
    def format(self,
               content: str,
               metadata: T,
               options: Optional[FoldingOptions] = None) -> str:
        """格式化内容

        Args:
            content: 需要格式化的内容
            metadata: 类型化的元数据
            options: 折叠选项

        Returns:
            str: 格式化后的内容

        Raises:
            FormattingError: 格式化过程中的错误
        """
        pass

    def _create_summary(self, metadata: T) -> str:
        """创建折叠摘要，可被子类重写"""
        return str(metadata)

    def _format_content_block(self,
                              content: str,
                              options: Optional[FoldingOptions]) -> str:
        """格式化内容块，处理代码块等特殊格式"""
        if not options:
            return content

        if options.code_language:
            return f"```{options.code_language}\n{content}\n```"
        return content

    def _add_indent(self, text: str, level: int) -> str:
        """添加缩进"""
        if level <= 0:
            return text
        indent = "  " * level
        return "\n".join(indent + line for line in text.splitlines())


class FileContentFormatter(ContentFormatter[FileMetadata]):
    """文件内容格式化器"""

    def format(self,
               content: str,
               metadata: FileMetadata,
               options: Optional[FoldingOptions] = None) -> str:
        """格式化文件内容"""
        if not metadata.validate():
            raise MetadataError("Invalid file metadata")

        try:
            options = options or FoldingOptions()

            # 构建摘要信息
            summary_parts = [
                f"{metadata.rel_path} ({metadata.size:.2f}MB)",
                f"Type: {metadata.mime_type}" if metadata.mime_type else None,
                (f"Modified: {metadata.last_modified.strftime('%Y-%m-%d %H:%M:%S')}"
                 if metadata.last_modified and options.show_timestamp else None)
            ]
            summary = " | ".join(filter(None, summary_parts))

            # 构建HTML类
            css_class = f' class="{options.custom_css}"' if options.custom_css else ''

            # 格式化内容
            formatted_content = self._format_content_block(content, options)

            # 组装最终结果
            result = (
                f'<details{css_class}><summary>{summary}</summary>\n\n'
                f'{formatted_content}\n\n'
                f'</details>\n\n'
            )

            return self._add_indent(result, options.indent_level)

        except Exception as e:
            logger.error(f"Error formatting file content: {str(e)}")
            raise FormattingError(f"Failed to format file content: {str(e)}")


class ContentFoldingManager:
    """内容折叠管理器"""

    def __init__(self):
        """初始化折叠管理器"""
        self._formatters: Dict[str, ContentFormatter] = {}
        self._register_default_formatters()

    def _register_default_formatters(self) -> None:
        """注册默认的格式化器"""
        self.register_formatter('file', FileContentFormatter())

    def register_formatter(self, name: str, formatter: ContentFormatter) -> None:
        """注册新的格式化器"""
        if not isinstance(formatter, ContentFormatter):
            raise TypeError("Formatter must implement ContentFormatter interface")
        self._formatters[name] = formatter

    def _guess_language(self, extension: str) -> Optional[str]:
        """根据文件扩展名猜测编程语言"""
        extension = extension.lower().lstrip('.')
        language_map = {
            'py': 'python',
            'js': 'javascript',
            'java': 'java',
            'cpp': 'cpp',
            'cs': 'csharp',
            'html': 'html',
            'css': 'css',
            'md': 'markdown',
            'json': 'json',
            'xml': 'xml',
            'sql': 'sql',
            'sh': 'bash',
            'yaml': 'yaml',
            'yml': 'yaml',
            'txt': None  # 纯文本不需要语言标识
        }
        return language_map.get(extension)

    def format_content(self,
                       content: str,
                       formatter_type: str,
                       metadata: Union[FileMetadata],
                       options: Optional[FoldingOptions] = None) -> str:
        """格式化内容"""
        formatter = self._formatters.get(formatter_type)
        if not formatter:
            raise KeyError(f"No formatter registered for type: {formatter_type}")

        if not isinstance(metadata, FileMetadata):
            raise TypeError("Invalid metadata type")

        return formatter.format(content, metadata, options)


@dataclass
class PaperMetadata(BaseMetadata):
    """论文元数据"""
    title: str
    authors: str
    abstract: str
    catalogs: str
    arxiv_id: str = ""

    def validate(self) -> bool:
        """验证论文元数据的有效性"""
        try:
            if not self._validate_non_empty_str(self.title):
                return False
            if not self._validate_non_empty_str(self.authors):
                return False
            if not self._validate_non_empty_str(self.abstract):
                return False
            if not self._validate_non_empty_str(self.catalogs):
                return False
            return True
        except Exception as e:
            logger.error(f"Paper metadata validation error: {str(e)}")
            return False


class PaperContentFormatter(ContentFormatter[PaperMetadata]):
    """论文内容格式化器"""

    def format(self,
               fragments: list[SectionFragment],
               metadata: PaperMetadata,
               options: Optional[FoldingOptions] = None) -> str:
        """格式化论文内容

        Args:
            fragments: 论文片段列表
            metadata: 论文元数据
            options: 折叠选项

        Returns:
            str: 格式化后的论文内容
        """
        if not metadata.validate():
            raise MetadataError("Invalid paper metadata")

        try:
            options = options or FoldingOptions()

            # 1. 生成标题部分（不折叠）
            result = [f"# {metadata.title}\n"]

            # 2. 生成作者信息（折叠）
            result.append(self._create_folded_section(
                "Authors",
                metadata.authors,
                options
            ))

            # 3. 生成摘要（折叠）
            result.append(self._create_folded_section(
                "Abstract",
                metadata.abstract,
                options
            ))

            # 4. 生成目录树（折叠）
            result.append(self._create_folded_section(
                "Table of Contents",
                f"```\n{metadata.catalogs}\n```",
                options
            ))

            # 5. 按章节组织并生成内容
            sections = self._organize_sections(fragments)
            for section, section_fragments in sections.items():
                # 拼接该章节的所有内容
                section_content = "\n\n".join(
                    fragment.content for fragment in section_fragments
                )

                result.append(self._create_folded_section(
                    section,
                    section_content,
                    options
                ))

            # 6. 生成参考文献（折叠）
            # 收集所有非空的参考文献
            all_refs = "\n".join(filter(None,
                                        (fragment.bibliography for fragment in fragments)
                                        ))
            if all_refs:
                result.append(self._create_folded_section(
                    "Bibliography",
                    f"```bibtex\n{all_refs}\n```",
                    options
                ))

            return "\n\n".join(result)

        except Exception as e:
            logger.error(f"Error formatting paper content: {str(e)}")
            raise FormattingError(f"Failed to format paper content: {str(e)}")

    def _create_folded_section(self,
                               title: str,
                               content: str,
                               options: FoldingOptions) -> str:
        """创建折叠区块

        Args:
            title: 区块标题
            content: 区块内容
            options: 折叠选项

        Returns:
            str: 格式化后的折叠区块
        """
        css_class = f' class="{options.custom_css}"' if options.custom_css else ''

        result = (
            f'<details{css_class}><summary>{title}</summary>\n\n'
            f'{content}\n\n'
            f'</details>'
        )

        return self._add_indent(result, options.indent_level)

    def _organize_sections(self,
                           fragments: list[SectionFragment]
                           ) -> Dict[str, list[SectionFragment]]:
        """将片段按章节分组

        Args:
            fragments: 论文片段列表

        Returns:
            Dict[str, list[SectionFragment]]: 按章节分组的片段字典
        """
        sections: Dict[str, list[SectionFragment]] = {}

        for fragment in fragments:
            section = fragment.current_section or "Uncategorized"
            if section not in sections:
                sections[section] = []
            sections[section].append(fragment)

        return sections