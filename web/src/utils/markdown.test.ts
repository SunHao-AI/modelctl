import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './markdown';

describe('renderMarkdown', () => {
  it('渲染列表与代码块', () => {
    const html = renderMarkdown('- a\n- b\n```js\nconst x = 1;\n```');
    expect(html).toContain('<li>a</li>');
    expect(html).toContain('hljs');
  });

  it('上游内容不可信：script 与事件属性必须被消毒', () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)">\n<script>alert(2)<\/script>');
    expect(html.toLowerCase()).not.toContain('<script');
    expect(html.toLowerCase()).not.toContain('onerror');
  });

  it('空输入返回空串而不抛', () => {
    expect(renderMarkdown('')).toBe('');
  });
});
