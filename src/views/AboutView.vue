<script setup lang="ts">
const envInfo = [
  { label: '环境标识', value: import.meta.env.VITE_APP_ENV },
  { label: '应用标题', value: import.meta.env.VITE_APP_TITLE },
  { label: '接口前缀', value: import.meta.env.VITE_APP_BASE_API },
  { label: '代理目标', value: import.meta.env.VITE_PROXY_TARGET || '-' },
  { label: '运行模式', value: import.meta.env.MODE },
  { label: '是否生产环境', value: String(import.meta.env.PROD) },
]
</script>

<template>
  <el-card shadow="never">
    <template #header>当前环境变量</template>
    <el-descriptions :column="1" border>
      <el-descriptions-item v-for="item in envInfo" :key="item.label" :label="item.label">
        {{ item.value }}
      </el-descriptions-item>
    </el-descriptions>
  </el-card>
</template>
