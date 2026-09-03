<script setup lang="ts">
// 根组件：仅承载路由出口，附带淡入淡出转场
</script>

<template>
  <router-view v-slot="{ Component }">
    <transition
      name="fade"
      mode="out-in"
      appear
    >
      <component :is="Component" />
    </transition>
  </router-view>
</template>
