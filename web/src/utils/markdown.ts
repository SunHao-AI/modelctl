import MarkdownIt, { type MarkdownIt as MarkdownItInstance } from 'markdown-it';
import DOMPurify from 'dompurify';
import hljs from 'highlight.js/lib/common';
import 'highlight.js/styles/github-dark.css';

/**
 * 对话正文 markdown 渲染（唯一使用点，便于以后换实现）。
 *
 * 上游模型输出是**不可信内容**：必须经 DOMPurify 消毒（script、事件属性
 * 在此被剥离）后才能交给 v-html。故这里允许内联 HTML 以便正常渲染模型
 * 给出的标签，安全边界完全落在下方的 sanitize 上。
 */
const md: MarkdownItInstance = new MarkdownIt({
  html: true,
  linkify: true,
  breaks: true,
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code class="language-${lang}">${hljs.highlight(code, { language: lang }).value}</code></pre>`;
      } catch {
        /* 高亮失败落到转义分支，绝不因渲染问题打断流式显示 */
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(code)}</code></pre>`;
  },
});

export function renderMarkdown(text: string): string {
  if (!text) return '';
  return DOMPurify.sanitize(md.render(text), { ADD_ATTR: ['target'] });
}
