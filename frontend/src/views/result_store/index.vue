<template>
  <section class="page" data-module="result_store">
    <header class="page-head">
      <div>
        <h2>采集仓储管理</h2>
        <p class="page-desc">维护采集结果的存储批次、完整性校验与版本追溯，保证原始数据可回溯。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记结果批次</button>
        <button class="btn" data-testid="reconcile" type="button" :disabled="reconciling" @click="reconcile">
          {{ reconciling ? '重算中…' : '状态重算' }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出采集仓储清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="applyFilters">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>批次编号</span>
        <input v-model="keyword" data-testid="filter-keyword" placeholder="按批次编号检索" />
      </label>
      <label class="filter-item">
        <span>当前状态</span>
        <select v-model="statusFilter" data-testid="filter-status">
          <option value="">全部状态</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td :data-testid="`status-${row.id}`">{{ row.status ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`changes-${row.id}`" @click="openChanges(row)">
              变更记录
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集仓储数据，可先登记结果批次</td>
        </tr>
      </tbody>
    </table>

    <div class="pager" data-testid="pager">
      <button class="btn ghost" type="button" data-testid="prev-page" :disabled="page <= 1" @click="goPage(page - 1)">
        上一页
      </button>
      <span data-testid="page-info">第 {{ page }} / {{ totalPages }} 页</span>
      <button class="btn ghost" type="button" data-testid="next-page" :disabled="page >= totalPages" @click="goPage(page + 1)">
        下一页
      </button>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记结果批次</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="changeLogBatch" class="modal-backdrop" data-testid="change-log-modal">
      <div class="modal">
        <h3>状态变更记录 · {{ changeLogBatch['批次编号'] }}</h3>
        <p class="page-desc">
          当前状态：{{ changeLogBatch.status }}；每次变更都记录原状态、目标状态、动作与原因。
          <RouterLink class="link" to="/quality_check" data-testid="goto-quality-check">前往质量审计清单</RouterLink>
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>变更时间</th>
              <th>动作</th>
              <th>原状态</th>
              <th>目标状态</th>
              <th>变更原因</th>
              <th>关联审计</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in changeLogs" :key="String(record.id)" :data-testid="`change-${record.id}`">
              <td>{{ record.changed_at }}</td>
              <td>{{ record.action }}</td>
              <td>{{ record.from_status }}</td>
              <td>{{ record.to_status }}</td>
              <td>{{ record.reason }}</td>
              <td>{{ record.audit_entry_id ? `审计记录 #${record.audit_entry_id}` : '—' }}</td>
            </tr>
            <tr v-if="!changeLogs.length">
              <td colspan="6" class="empty-state">暂无状态变更记录</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions" style="margin-top: 12px">
          <button class="btn" type="button" data-testid="close-change-log" @click="changeLogBatch = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集仓储记录</span>
      <span v-if="noticeMessage" class="notice-text" data-testid="notice">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/result_store'
const columns = ["批次编号", "关联任务", "原始行数", "有效行数", "存储路径", "压缩格式", "校验摘要", "落地时间"]
const actions = ["触发校验", "标记通过", "标记失败"]
const statuses = ["接收中", "已落盘", "校验通过", "校验失败"]
const metricNames = ["批次总数", "校验通过批次", "校验失败批次"]
// 与后端状态机一致：每个当前状态只放出合法动作，非法跳转在页面上就不出现
const actionMatrix: Record<string, string[]> = {
  接收中: ["触发校验"],
  已落盘: ["标记通过", "标记失败"],
  校验通过: [],
  校验失败: [],
}

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const keyword = ref('')
const statusFilter = ref('')
const passTotal = ref(0)
const failTotal = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const reconciling = ref(false)
const createValues = ref<Record<string, string>>({})
const changeLogBatch = ref<Row | null>(null)
const changeLogs = ref<Row[]>([])

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: passTotal.value },
  { label: metricNames[2], value: failTotal.value },
])

function availableActions(row: Row) {
  return actionMatrix[String(row.status ?? '')] ?? []
}

function buildQuery() {
  const query = new URLSearchParams(filters.value as Record<string, string>)
  if (keyword.value.trim()) query.set('keyword', keyword.value.trim())
  if (statusFilter.value) query.set('status', statusFilter.value)
  query.set('page', String(page.value))
  query.set('size', String(size.value))
  return query.toString()
}

async function fetchStatusTotal(status: string) {
  const query = new URLSearchParams({ status, page: '1', size: '1' })
  const response = await request(`${ENDPOINT}?${query.toString()}`)
  if (!response.ok) throw new Error('状态统计读取失败')
  const payload = await response.json()
  return Number(payload.total ?? 0)
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}?${buildQuery()}`)
    if (!response.ok) {
      throw new Error('结果批次列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    const [passed, failed] = await Promise.all([
      fetchStatusTotal('校验通过'),
      fetchStatusTotal('校验失败'),
    ])
    passTotal.value = passed
    failTotal.value = failed
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集仓储列表读取失败'
  }
}

function applyFilters() {
  page.value = 1
  void reload()
}

function goPage(target: number) {
  if (target < 1 || target > totalPages.value) return
  page.value = target
  void reload()
}

function resetFilters() {
  filters.value = {}
  keyword.value = ''
  statusFilter.value = ''
  page.value = 1
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'result_store.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '采集仓储动作未生效，请稍后重试')
    }
    noticeMessage.value = payload.message || `结果批次已${action}`
    await reload()
    if (changeLogBatch.value) await openChanges(changeLogBatch.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集仓储操作失败'
  }
}

async function openChanges(row: Row) {
  errorMessage.value = ''
  changeLogBatch.value = row
  try {
    const response = await request(`${ENDPOINT}/${row.id}/status_changes`)
    if (!response.ok) throw new Error('状态变更记录读取失败')
    const payload = await response.json()
    changeLogs.value = payload.items ?? []
  } catch (error) {
    changeLogs.value = []
    errorMessage.value = error instanceof Error ? error.message : '状态变更记录读取失败'
  }
}

async function reconcile() {
  reconciling.value = true
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/reconcile`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '状态重算失败')
    noticeMessage.value = payload.message || '状态重算完成'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '状态重算失败'
  } finally {
    reconciling.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  gap: 12px;
  justify-content: flex-end;
  margin-top: 8px;
  font-size: 12px;
  color: var(--muted);
}

.notice-text {
  color: #15803d;
}
</style>
