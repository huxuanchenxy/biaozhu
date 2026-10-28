<script setup lang="ts">
/**
 * 401 未授权页：本平台仅允许通过具体的标注任务链接（/cad/<key> 或 /markdown/<key>）进入。
 * 直接访问根目录/首页等「前台入口」由路由守卫统一拦截并跳到此页，提示未授权。
 */
</script>

<template>
  <div class="unauthorized">
    <el-result icon="error" title="401" sub-title="未授权访问：请通过具体的标注任务链接进入">
      <template #extra>
        <div class="hint">当前地址不是有效的标注任务入口。</div>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.unauthorized {
  /* 撑满抬头以外的可视区，让 401 提示垂直居中 */
  min-height: calc(100vh - var(--app-header-height));
  display: flex;
  align-items: center;
  justify-content: center;
}

.hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
