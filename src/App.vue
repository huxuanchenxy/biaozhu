<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { currentTheme, toggleTheme, type ThemeMode } from '@/utils/theme'

const appTitle = import.meta.env.VITE_APP_TITLE

const menus = [
  { path: '/home', title: '首页' },
  { path: '/about', title: '关于' },
]

const route = useRoute()

/** 标注页只保留抬头（logo），不显示导航 tab，内容区也撑满整屏 */
const hideNav = computed(() => route.meta.hideNav === true)

/** 当前主题（初始值由首屏引导脚本写在 <html> 上，这里读取保持一致） */
const theme = ref<ThemeMode>(currentTheme())
function switchTheme() {
  theme.value = toggleTheme()
}
</script>

<template>
  <el-container class="layout">
    <el-header class="layout-header">
      <div class="logo">{{ appTitle }}</div>

      <el-menu
        v-if="!hideNav"
        :default-active="route.path"
        mode="horizontal"
        router
        :ellipsis="false"
        class="layout-menu"
      >
        <el-menu-item v-for="item in menus" :key="item.path" :index="item.path">
          {{ item.title }}
        </el-menu-item>
      </el-menu>
      <div v-else class="layout-menu layout-menu--empty"></div>

      <el-tooltip :content="theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'" placement="bottom">
        <el-button class="theme-toggle" circle @click="switchTheme">
          <el-icon><component :is="theme === 'dark' ? 'Sunny' : 'Moon'" /></el-icon>
        </el-button>
      </el-tooltip>
    </el-header>

    <el-main :class="['layout-main', { 'layout-main--flush': hideNav }]">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100%;
}

.layout-header {
  display: flex;
  align-items: center;
  height: var(--app-header-height);
  padding: 0 24px;
  background-color: var(--c-bg-header);
  box-shadow: 0 1px 4px var(--c-shadow);
}

.logo {
  font-size: 18px;
  font-weight: 600;
  white-space: nowrap;
}

.layout-menu {
  flex: 1;
  margin-left: 32px;
  border-bottom: none;
}

.theme-toggle {
  flex: none;
  margin-left: 16px;
}

.layout-main {
  padding: 20px;
}

.layout-main--flush {
  padding: 0;
  overflow: hidden;
}
</style>
