<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getSessionList, getSkillList } from '@/api'
import type { SessionItem, SkillItem } from '@/api/types'

const loading = ref(false)
const sessions = ref<SessionItem[]>([])
const skills = ref<SkillItem[]>([])
const errorMsg = ref('')

/** 后端可能直接返回数组，也可能包一层 { code, data, message }，这里做兼容 */
function unwrap<T>(res: any): T[] {
  if (Array.isArray(res)) return res
  if (Array.isArray(res?.data)) return res.data
  return []
}

const fetchData = async () => {
  loading.value = true
  errorMsg.value = ''
  try {
    const [sessionRes, skillRes] = await Promise.all([getSessionList(), getSkillList()])
    sessions.value = unwrap<SessionItem>(sessionRes)
    skills.value = unwrap<SkillItem>(skillRes)
  } catch (e: any) {
    errorMsg.value = e?.message || '接口请求失败，请检查后端服务是否已启动'
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)
</script>

<template>
  <div class="home">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>接口连通性示例</span>
          <el-button type="primary" :loading="loading" @click="fetchData">
            <el-icon><Refresh /></el-icon>
            重新请求
          </el-button>
        </div>
      </template>

      <el-alert
        v-if="errorMsg"
        :title="errorMsg"
        type="error"
        show-icon
        :closable="false"
        class="mb"
      />

      <el-row :gutter="16">
        <el-col :span="12">
          <h3 class="block-title">会话列表 /api/session/list</h3>
          <el-table
            v-loading="loading"
            :data="sessions"
            border
            size="small"
            max-height="360"
          >
            <el-table-column prop="sessionId" label="会话 ID" min-width="220" />
            <el-table-column prop="name" label="名称" min-width="180" />
            <el-table-column prop="chatTime" label="时间" min-width="180" />
            <template #empty>暂无数据</template>
          </el-table>
        </el-col>

        <el-col :span="12">
          <h3 class="block-title">技能列表 /api/skill/list</h3>
          <el-table v-loading="loading" :data="skills" border size="small" max-height="360">
            <el-table-column type="index" label="#" width="60" />
            <el-table-column prop="name" label="技能名称" min-width="180" />
            <el-table-column prop="description" label="描述" min-width="200" />
            <template #empty>暂无数据</template>
          </el-table>
        </el-col>
      </el-row>
    </el-card>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.block-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
}

.mb {
  margin-bottom: 16px;
}
</style>
