<script setup lang="ts">
/**
 * 精修表格容器：外层卡片 + 统一表头/行/悬停样式。
 * 包裹式而非配置式 —— 各视图保留自己的 <table> 标记与列定义，零逻辑改动。
 */
</script>

<template>
  <div class="overflow-hidden rounded-card border border-sep bg-surface2" style="box-shadow: var(--shadow-s)">
    <div class="overflow-x-auto">
      <slot />
    </div>
  </div>
</template>

<style scoped>
/* :slotted() 只匹配插槽的**顶层**节点（这里是 <table>），其后代必须写在括号外面。
   写成 :slotted(tbody tr) 不会命中 —— tbody 不是被直接插入插槽的节点。 */
:slotted(table) {
  width: 100%;
  border-collapse: collapse;
}
/* QA B-04：默认左对齐必须包 :where() 归零特异度（scoped 编译为
   `:where(table[data-v-x] th)`，特异度 0），否则本规则 (0,1,1) 稳压模板
   `.text-right` (0,1,0)，右对齐列的表头被拉左、与右对齐单元格错位。
   未写对齐类的 th 仍吃到这里的 left（作者样式恒胜 UA 的 center）。 */
:where(:slotted(table) th) {
  text-align: left;
}
:slotted(table) th {
  font-size: 11px;
  font-weight: 620;
  letter-spacing: 0.055em;
  text-transform: uppercase;
  color: var(--label-3);
  padding: 10px 14px;
  background: var(--surface-3);
  border-bottom: 0.5px solid var(--separator);
  white-space: nowrap;
}
:slotted(table) td {
  padding: 11px 14px;
  font-size: 13px;
  color: var(--label);
  border-bottom: 0.5px solid var(--separator-soft);
}
:slotted(table) tbody tr {
  transition: background 0.14s var(--ease);
}
:slotted(table) tbody tr:hover {
  background: var(--surface-3);
}
:slotted(table) tbody tr:last-child td {
  border-bottom: 0;
}
</style>
