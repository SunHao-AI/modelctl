/**
 * QA B-04 契约：DataTable 的「表头默认左对齐」是组件基元，但不得压掉
 * 模板里显式写的 `.text-right`（scoped 编译后 `table[data-v-x] th` 特异度
 * (0,1,1) > `.text-right` (0,1,0)，会把右对齐表头拉回左对齐）。
 * jsdom 不注入 scoped 样式，所以断言打在**编译产物**上：
 * 所有含 text-align 的规则必须包在 :where(…th…) 里（特异度归零）。
 */
import { parse, compileStyle } from 'vue/compiler-sfc';
import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import DataTable from './DataTable.vue';
// ?raw：jsdom 下 import.meta.url 非 file: 协议，不能用 readFileSync + fileURLToPath
import sfcSource from './DataTable.vue?raw';

const { descriptor } = parse(sfcSource);
const compiled = compileStyle({
  source: descriptor.styles[0].content,
  filename: 'DataTable.vue',
  id: 'data-v-test',
  scoped: true,
});

it('scoped 编译无错误', () => {
  expect(compiled.errors).toHaveLength(0);
});

it('默认对齐规则包在 :where() 中（0 特异度），放行模板 .text-right', () => {
  const code = compiled.code.replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [...code.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(([, , decl]) =>
    /text-align/.test(decl),
  );
  expect(rules.length, '找不到 text-align 规则 —— 默认左对齐被删了？').toBeGreaterThan(0);
  for (const [, selector] of rules) {
    expect(selector.trim(), `选择器 ${selector.trim()} 特异度非 0，会压掉 .text-right`).toMatch(/^:where\([^)]*\bth\b[^)]*\)$/);
  }
});

it('模板写 text-right 的 th 保留右对齐类，未写的无对齐类（由 :where 兜底左对齐）', () => {
  const wrapper = mount(DataTable, {
    slots: {
      default: '<table><thead><tr><th>状态</th><th class="text-right">端口</th></tr></thead></table>',
    },
  });
  const ths = wrapper.findAll('th');
  expect(ths[0].classes()).not.toContain('text-right');
  expect(ths[1].classes()).toContain('text-right');
});
