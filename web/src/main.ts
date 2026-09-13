import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import router from './router';
import { useThemeStore } from './stores/theme';
import 'uno.css';
import './styles/tokens.css';
import './styles/global.css';

const app = createApp(App);
app.use(createPinia());
// 与 index.html 内联脚本结果对齐（内联脚本可能因隐私模式读不到 localStorage）
useThemeStore().apply();
app.use(router);
app.mount('#app');
