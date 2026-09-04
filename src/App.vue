<script setup lang="ts">
import { useRoute } from 'vue-router'

const appTitle = import.meta.env.VITE_APP_TITLE
const appEnv = import.meta.env.VITE_APP_ENV

const menus = [
  { path: '/home', title: '首页' },
  { path: '/about', title: '关于' },
]

const route = useRoute()
</script>

<template>
  <el-container class="layout">
    <el-header class="layout-header">
      <div class="logo">{{ appTitle }}</div>

      <el-menu
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

      <div class="env-tag">
        <el-tag :type="appEnv === 'production' ? 'success' : 'warning'" size="small">
          {{ appEnv }}
        </el-tag>
      </div>
    </el-header>

    <el-main class="layout-main">
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
  background-color: #fff;
  box-shadow: 0 1px 4px rgba(0, 21, 41, 0.08);
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

.env-tag {
  white-space: nowrap;
}

.layout-main {
  padding: 20px;
}
</style>
