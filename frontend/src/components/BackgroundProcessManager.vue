<template>
  <div class="process-manager">
    <button
      v-if="!isOpen"
      class="process-launcher"
      type="button"
      :aria-label="`Open process manager. ${activeCount} active`"
      @click="openManager"
    >
      <span class="launcher-pulse" :class="{ idle: activeCount === 0 }"></span>
      <span class="launcher-label">PROCESSES</span>
      <span class="launcher-count">{{ activeCount }}</span>
    </button>

    <div v-if="isOpen" class="manager-overlay" @click.self="isOpen = false">
      <section
        class="manager-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="process-manager-title"
      >
        <header class="manager-header">
          <div>
            <div class="eyebrow">SYSTEM MONITOR</div>
            <h1 id="process-manager-title">Process manager</h1>
            <p class="header-caption">Track and manage background work in one place.</p>
          </div>
          <button class="icon-button close-button" type="button" aria-label="Close" @click="isOpen = false">
            ×
          </button>
        </header>

        <div class="summary-row">
          <div class="summary-card active-summary">
            <span class="summary-label">ACTIVE NOW</span>
            <strong>{{ activeCount }}</strong>
            <span class="summary-footnote">active processes</span>
          </div>
          <div class="summary-card">
            <span class="summary-label">HISTORY</span>
            <strong>{{ historyItems.length }}</strong>
            <span class="summary-footnote">saved work items</span>
          </div>
          <button
            class="icon-button refresh-button"
            type="button"
            :disabled="isLoading"
            aria-label="Refresh processes"
            title="Refresh"
            @click="refreshProcesses"
          >
            <span :class="{ spinning: isLoading }">↻</span>
          </button>
        </div>

        <div class="manager-toolbar">
          <div class="manager-tabs" role="tablist" aria-label="Process views">
            <button
              type="button"
              role="tab"
              :aria-selected="selectedTab === 'active'"
              :class="{ selected: selectedTab === 'active' }"
              @click="selectedTab = 'active'"
            >
              Active <span>{{ activeCount }}</span>
            </button>
            <button
              type="button"
              role="tab"
              :aria-selected="selectedTab === 'history'"
              :class="{ selected: selectedTab === 'history' }"
              @click="selectedTab = 'history'"
            >
              History <span>{{ historyItems.length }}</span>
            </button>
          </div>
          <button
            v-if="selectedTab === 'history' && historyItems.length"
            class="clear-button"
            type="button"
            :disabled="isClearing"
            @click="clearHistory"
          >
            {{ isClearing ? 'Clearing...' : 'Clear history' }}
          </button>
        </div>

        <div v-if="refreshError" class="manager-error" role="alert">
          <span>{{ refreshError }}</span>
          <button type="button" @click="refreshProcesses">Retry</button>
        </div>
        <div v-if="actionError" class="manager-error action-error" role="alert">
          {{ actionError }}
        </div>

        <div class="process-list" aria-live="polite">
          <div v-if="isLoading && !hasLoaded" class="empty-state">
            <span class="loading-mark"></span>
            <strong>Connecting to backend</strong>
            <p>Loading background tasks and running simulations.</p>
          </div>
          <div v-else-if="displayedItems.length === 0" class="empty-state">
            <span class="empty-icon">{{ selectedTab === 'active' ? '✓' : '⌁' }}</span>
            <strong>
              {{ refreshError
                ? 'Process list is incomplete'
                : selectedTab === 'active' ? 'No active processes' : 'No history yet' }}
            </strong>
            <p>
              {{ refreshError
                ? 'Some process sources are unavailable. Retry to get a complete list.'
                : selectedTab === 'active'
                  ? 'There is no background work running right now.'
                  : 'Completed, failed, cancelled, and interrupted work will appear here.' }}
            </p>
          </div>

          <article
            v-for="item in displayedItems"
            :key="item.key"
            class="process-card"
            :class="{
              'is-cancelling': item.status === 'cancelling',
              'is-clickable': item.isActive && item.canOpen
            }"
            :role="item.isActive && item.canOpen ? 'link' : undefined"
            :tabindex="item.isActive && item.canOpen ? 0 : undefined"
            @click="openProcess(item)"
            @keydown="handleCardKeydown(item, $event)"
          >
            <div class="card-topline">
              <span class="process-kind">
                <span class="kind-mark" :class="item.isSimulation ? 'simulation-mark' : ''">
                  {{ item.isSimulation ? '↗' : '◈' }}
                </span>
                {{ item.isSimulation ? 'SIMULATION' : 'TASK' }}
              </span>
              <span class="status-badge" :class="`status-${item.status}`">
                <span class="status-dot"></span>
                {{ statusLabel(item.status) }}
              </span>
            </div>

            <div class="card-title-row">
              <div class="card-title-block">
                <h2>{{ item.title }}</h2>
                <p class="process-id">{{ item.idLabel }} {{ item.id }}</p>
              </div>
              <span v-if="item.isActive && item.canOpen" class="open-hint">Open process ↗</span>
              <button
                v-if="item.isActive"
                class="stop-button"
                type="button"
                :disabled="busyKey === item.key || item.status === 'cancelling'"
                @click.stop="stopProcess(item)"
              >
                {{ busyKey === item.key ? 'Stopping...' : item.isSimulation ? 'Stop' : 'Cancel' }}
              </button>
              <button
                v-if="item.canResume"
                class="stop-button resume-button"
                type="button"
                :disabled="busyKey === item.key"
                @click.stop="resumeBackgroundTask(item)"
              >
                {{ busyKey === item.key ? 'Resuming...' : 'Resume' }}
              </button>
              <button
                v-if="item.canRestart"
                class="stop-button resume-button"
                type="button"
                :disabled="busyKey === item.key"
                @click.stop="restartSimulation(item)"
              >
                {{ busyKey === item.key ? 'Restarting...' : 'Restart from beginning' }}
              </button>
            </div>

            <div class="progress-label-row">
              <span class="progress-message">{{ item.message || 'Waiting for an update...' }}</span>
              <strong>{{ formatProgress(item.progress) }}%</strong>
            </div>
            <div
              class="progress-track"
              role="progressbar"
              :aria-valuenow="formatProgress(item.progress)"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-label="`Progress for ${item.title}`"
            >
              <div
                class="progress-fill"
                :class="{ indeterminate: item.status === 'cancelling' }"
                :style="{ width: `${formatProgress(item.progress)}%` }"
              ></div>
            </div>

            <footer class="card-footer">
              <span>{{ item.runtimeLabel }}</span>
              <span>{{ item.threadLabel }}</span>
            </footer>
          </article>
        </div>

        <footer class="manager-footer">
          <span class="live-indicator"><span></span> Auto-refresh every 3 s</span>
          <span v-if="lastUpdated">Updated {{ lastUpdated }}</span>
        </footer>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { cancelTask, clearTaskHistory, listTasks, resumeTask } from '../api/tasks'
import {
  listRunningSimulations,
  listSimulationRunHistory,
  startSimulation,
  stopSimulation
} from '../api/simulation'

const router = useRouter()
const isOpen = ref(false)
const selectedTab = ref('active')
const tasks = ref([])
const simulations = ref([])
const simulationHistory = ref([])
const isLoading = ref(false)
const hasLoaded = ref(false)
const isClearing = ref(false)
const busyKey = ref('')
const refreshError = ref('')
const actionError = ref('')
const lastUpdated = ref('')
let pollingTimer = null

const activeTaskStatuses = ['pending', 'processing', 'cancelling']
const terminalTaskStatuses = ['completed', 'failed', 'cancelled', 'interrupted']

const activeItems = computed(() => {
  const taskItems = tasks.value
    .filter(task => activeTaskStatuses.includes(task.status))
    .map(task => ({
      key: `task-${task.task_id}`,
      taskId: task.task_id,
      taskType: task.task_type,
      metadata: task.metadata,
      id: task.task_id,
      idLabel: 'ID',
      title: taskTitle(task),
      message: task.message,
      progress: task.progress,
      status: task.status,
      sortAt: task.created_at,
      isActive: true,
      isSimulation: false,
      canOpen: taskCanOpen(task),
      runtimeLabel: `Started ${formatDate(task.created_at)}`,
      threadLabel: task.thread_id
        ? `${task.thread_name || 'Thread'} #${task.thread_id}`
        : task.thread_alive ? 'Worker starting' : 'Worker unavailable'
    }))
  const simulationItems = simulations.value.map(simulation => ({
    key: `simulation-${simulation.simulation_id}`,
    simulationId: simulation.simulation_id,
    id: simulation.simulation_id,
    idLabel: 'ID',
    title: 'Simulation run',
    message: simulation.message,
    progress: simulation.progress_percent,
    status: 'processing',
    sortAt: simulation.started_at,
    isActive: true,
    isSimulation: true,
    canOpen: true,
    runtimeLabel: `Round ${simulation.current_round || 0} / ${simulation.total_rounds || '—'}`,
    threadLabel: simulation.process_pid ? `Process PID ${simulation.process_pid}` : 'Process active'
  }))

  return [...taskItems, ...simulationItems].sort((a, b) => dateValue(b.sortAt) - dateValue(a.sortAt))
})

const historyItems = computed(() => {
  const taskItems = tasks.value
    .filter(task => terminalTaskStatuses.includes(task.status))
    .map(task => ({
      key: `history-${task.task_id}`,
      id: task.task_id,
      idLabel: 'ID',
      title: taskTitle(task),
      message: task.error || task.message,
      progress: task.progress,
      status: task.status,
      sortAt: task.updated_at,
      isActive: false,
      isSimulation: false,
      canResume: Boolean(task.resumable && ['interrupted', 'failed'].includes(task.status)),
      runtimeLabel: `Started ${formatDate(task.created_at)}`,
      threadLabel: `Updated ${formatDate(task.updated_at)}`
    }))
  const simulationItems = simulationHistory.value.map(simulation => ({
    key: `simulation-history-${simulation.simulation_id}`,
    simulationId: simulation.simulation_id,
    id: simulation.simulation_id,
    idLabel: 'ID',
    title: 'Simulation run',
    message: simulation.message,
    progress: simulation.progress_percent,
    status: simulation.runner_status,
    sortAt: simulation.updated_at || simulation.completed_at || simulation.started_at,
    isActive: false,
    isSimulation: true,
    canRestart: ['interrupted', 'failed'].includes(simulation.runner_status),
    platform: simulation.platform,
    maxRounds: simulation.max_rounds,
    graphMemoryUpdateEnabled: simulation.graph_memory_update_enabled,
    runtimeLabel: `Round ${simulation.current_round || 0} / ${simulation.total_rounds || '—'}`,
    threadLabel: simulation.process_pid ? `Last process PID ${simulation.process_pid}` : 'Process ended'
  }))
  return [...taskItems, ...simulationItems].sort((a, b) => dateValue(b.sortAt) - dateValue(a.sortAt))
})

const displayedItems = computed(() => selectedTab.value === 'active' ? activeItems.value : historyItems.value)
const activeCount = computed(() => activeItems.value.length)

function taskTitle(task) {
  const titles = {
    graph_build: 'Graph build',
    simulation_prepare: 'Simulation preparation',
    report_generate: 'Report generation'
  }
  if (task.task_type === 'graph_build' && task.metadata?.graph_name) {
    return `Graph: ${task.metadata.graph_name}`
  }
  return titles[task.task_type] || task.task_type || 'Background task'
}

function taskCanOpen(task) {
  if (task.task_type === 'graph_build') return Boolean(task.metadata?.project_id)
  if (task.task_type === 'simulation_prepare') return Boolean(task.metadata?.simulation_id)
  if (task.task_type === 'report_generate') {
    return Boolean(task.metadata?.report_id && task.metadata?.simulation_id)
  }
  return false
}

function statusLabel(status) {
  return {
    pending: 'Queued',
    processing: 'Running',
    cancelling: 'Stopping',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    interrupted: 'Interrupted'
  }[status] || status
}

function formatProgress(value) {
  const numericValue = Number(value)
  return Number.isFinite(numericValue)
    ? Math.round(Math.min(100, Math.max(0, numericValue)) * 10) / 10
    : 0
}

function dateValue(value) {
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? 0 : parsed
}

function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '—'
    : date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

async function refreshProcesses() {
  if (isLoading.value) return
  isLoading.value = true
  try {
    const [taskResult, simulationResult, historyResult] = await Promise.allSettled([
      listTasks(),
      listRunningSimulations(),
      listSimulationRunHistory()
    ])
    const errors = []
    let receivedData = false

    if (taskResult.status === 'fulfilled') {
      tasks.value = taskResult.value.data || []
      receivedData = true
    } else {
      errors.push(`Tasks: ${taskResult.reason?.message || String(taskResult.reason)}`)
    }

    if (simulationResult.status === 'fulfilled') {
      simulations.value = simulationResult.value.data || []
      receivedData = true
    } else {
      errors.push(`Simulations: ${simulationResult.reason?.message || String(simulationResult.reason)}`)
    }

    if (historyResult.status === 'fulfilled') {
      simulationHistory.value = historyResult.value.data || []
      receivedData = true
    } else {
      errors.push(`Simulation history: ${historyResult.reason?.message || String(historyResult.reason)}`)
    }

    if (receivedData) {
      hasLoaded.value = true
      lastUpdated.value = new Date().toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      })
    }
    refreshError.value = errors.length
      ? `Could not refresh all process sources. Showing the last available data. ${errors.join(' ')}`
      : ''
  } catch (error) {
    refreshError.value = `Could not load processes: ${error.message}`
  } finally {
    isLoading.value = false
  }
}

async function openProcess(item) {
  if (!item.isActive || !item.canOpen) return

  isOpen.value = false
  actionError.value = ''
  if (item.isSimulation) {
    await router.push({
      name: 'SimulationRun',
      params: { simulationId: item.simulationId },
      query: { resume: 'true' }
    })
    return
  }

  const taskId = item.taskId
  if (item.taskType === 'graph_build') {
    await router.push({
      name: 'Process',
      params: { projectId: item.metadata.project_id },
      query: { taskId }
    })
  } else if (item.taskType === 'simulation_prepare') {
    await router.push({
      name: 'Simulation',
      params: { simulationId: item.metadata.simulation_id },
      query: { taskId }
    })
  } else if (item.taskType === 'report_generate') {
    await router.push({
      name: 'Report',
      params: { reportId: item.metadata.report_id },
      query: {
        simulationId: item.metadata.simulation_id,
        taskId
      }
    })
  }
}

function handleCardKeydown(item, event) {
  if (event.target !== event.currentTarget || !['Enter', ' '].includes(event.key)) return
  event.preventDefault()
  openProcess(item)
}

function openManager() {
  isOpen.value = true
  selectedTab.value = 'active'
  actionError.value = ''
  refreshProcesses()
}

async function stopProcess(item) {
  const prompt = item.isSimulation
    ? 'Stop this simulation process now?'
    : 'Request cancellation for this background task?'
  if (!window.confirm(prompt)) return

  busyKey.value = item.key
  actionError.value = ''
  try {
    if (item.isSimulation) {
      await stopSimulation({ simulation_id: item.simulationId })
    } else {
      await cancelTask(item.taskId)
    }
    await refreshProcesses()
  } catch (error) {
    actionError.value = `Could not stop the process: ${error.message}`
  } finally {
    busyKey.value = ''
  }
}

async function resumeBackgroundTask(item) {
  if (!window.confirm('Resume this task from its latest saved checkpoint? The interrupted step may need to run again.')) return

  busyKey.value = item.key
  actionError.value = ''
  try {
    await resumeTask(item.id)
    await refreshProcesses()
  } catch (error) {
    actionError.value = `Could not resume the task: ${error.message}`
  } finally {
    busyKey.value = ''
  }
}

async function restartSimulation(item) {
  if (!window.confirm('The OASIS in-memory state cannot be restored. Restart this simulation from the beginning?')) return

  busyKey.value = item.key
  actionError.value = ''
  try {
    await startSimulation({
      simulation_id: item.simulationId,
      platform: item.platform || 'parallel',
      max_rounds: item.maxRounds || undefined,
      enable_graph_memory_update: item.graphMemoryUpdateEnabled,
      force: true
    })
    await refreshProcesses()
  } catch (error) {
    actionError.value = `Could not restart the simulation: ${error.message}`
  } finally {
    busyKey.value = ''
  }
}

async function clearHistory() {
  if (!window.confirm('Clear finished background task records? Simulation run records and their logs will be kept.')) return

  isClearing.value = true
  actionError.value = ''
  try {
    await clearTaskHistory()
    await refreshProcesses()
  } catch (error) {
    actionError.value = `Could not clear history: ${error.message}`
  } finally {
    isClearing.value = false
  }
}

onMounted(() => {
  refreshProcesses()
  pollingTimer = window.setInterval(refreshProcesses, 3000)
})

onBeforeUnmount(() => {
  if (pollingTimer) window.clearInterval(pollingTimer)
})
</script>

<style scoped>
.process-manager {
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
}

.process-launcher {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 1900;
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 52px;
  padding: 0 16px;
  border: 0;
  border-radius: 12px;
  background: #101216;
  color: #fff;
  box-shadow: 0 12px 35px rgba(10, 15, 25, .26);
  cursor: pointer;
  transition: transform .2s ease, box-shadow .2s ease;
}

.process-launcher:hover {
  transform: translateY(-2px);
  box-shadow: 0 15px 40px rgba(10, 15, 25, .32);
}

.launcher-pulse,
.live-indicator span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #38D9A9;
  box-shadow: 0 0 0 4px rgba(56, 217, 169, .14);
  animation: pulse 1.8s infinite;
}

.launcher-pulse.idle {
  background: #87909C;
  box-shadow: none;
  animation: none;
}

.launcher-label {
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 1.2px;
}

.launcher-count {
  display: grid;
  min-width: 24px;
  height: 24px;
  padding: 0 6px;
  place-items: center;
  border-radius: 7px;
  background: #FF5722;
  font-size: 12px;
  font-weight: 800;
}

.manager-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  justify-content: flex-end;
  background: rgba(15, 20, 28, .48);
  backdrop-filter: blur(3px);
}

.manager-panel {
  display: flex;
  flex-direction: column;
  width: min(590px, 100%);
  height: 100%;
  padding: 30px 28px 18px;
  background: #F7F8FA;
  box-shadow: -16px 0 50px rgba(0, 0, 0, .14);
  animation: slide-in .22s ease-out;
}

.manager-header {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  padding-bottom: 22px;
}

.eyebrow {
  margin-bottom: 8px;
  color: #F05A35;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 1.3px;
}

.manager-header h1 {
  color: #101216;
  font-size: 25px;
  letter-spacing: -.8px;
}

.header-caption {
  margin-top: 6px;
  color: #757D89;
  font-size: 13px;
}

.icon-button {
  display: grid;
  flex: 0 0 auto;
  width: 38px;
  height: 38px;
  place-items: center;
  border: 1px solid #E1E4E9;
  border-radius: 10px;
  background: #fff;
  color: #48505B;
  cursor: pointer;
}

.icon-button:disabled {
  opacity: .55;
  cursor: wait;
}

.close-button {
  font-size: 25px;
  line-height: 1;
}

.summary-row {
  display: flex;
  gap: 12px;
  align-items: stretch;
  margin-bottom: 22px;
}

.summary-card {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 102px;
  padding: 13px 15px;
  border: 1px solid #E7E9ED;
  border-radius: 12px;
  background: #fff;
}

.active-summary {
  border-color: #D7F2E9;
  background: #F2FBF7;
}

.summary-label {
  color: #7A828D;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .8px;
}

.summary-card strong {
  margin: 4px 0 0;
  color: #11151A;
  font-size: 28px;
  line-height: 1;
}

.active-summary strong {
  color: #07875F;
}

.summary-footnote {
  margin-top: 5px;
  color: #838B96;
  font-size: 10px;
}

.refresh-button {
  align-self: flex-start;
  width: 42px;
  height: 42px;
  font-size: 22px;
}

.refresh-button span {
  display: inline-block;
}

.spinning {
  animation: spin .8s linear infinite;
}

.manager-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 46px;
  border-bottom: 1px solid #E5E8ED;
}

.manager-tabs {
  display: flex;
  align-self: stretch;
  gap: 20px;
}

.manager-tabs button {
  position: relative;
  display: flex;
  align-items: center;
  gap: 7px;
  border: 0;
  background: transparent;
  color: #808793;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.manager-tabs button.selected {
  color: #171B21;
}

.manager-tabs button.selected::after {
  position: absolute;
  right: 0;
  bottom: -1px;
  left: 0;
  height: 2px;
  background: #F05A35;
  content: '';
}

.manager-tabs button span {
  color: #9299A3;
  font-size: 10px;
}

.clear-button {
  padding: 7px 10px;
  border: 1px solid #E6B7AD;
  border-radius: 7px;
  background: #FFF9F7;
  color: #C4482B;
  font-size: 10px;
  font-weight: 700;
  cursor: pointer;
}

.clear-button:disabled {
  opacity: .6;
  cursor: wait;
}

.manager-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid #F3C6BE;
  border-radius: 9px;
  background: #FFF3F0;
  color: #A43520;
  font-size: 11px;
}

.manager-error button {
  flex: 0 0 auto;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: 11px;
  font-weight: 800;
  cursor: pointer;
}

.action-error {
  justify-content: flex-start;
}

.process-list {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 16px 3px 20px 0;
}

.process-card {
  margin-bottom: 12px;
  padding: 15px 16px 12px;
  border: 1px solid #E3E6EB;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 3px 10px rgba(24, 35, 50, .025);
}

.process-card.is-clickable {
  cursor: pointer;
  transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease;
}

.process-card.is-clickable:hover,
.process-card.is-clickable:focus-visible {
  transform: translateY(-1px);
  border-color: #F0A08A;
  box-shadow: 0 8px 20px rgba(24, 35, 50, .08);
  outline: none;
}

.process-card.is-cancelling {
  border-color: #F2D8A4;
  background: #FFFCF5;
}

.card-topline,
.card-title-row,
.progress-label-row,
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.process-kind {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #838B96;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .65px;
}

.kind-mark {
  color: #F05A35;
  font-size: 14px;
}

.kind-mark.simulation-mark {
  color: #635BDF;
}

.status-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-radius: 20px;
  background: #EDF9F3;
  color: #18845F;
  font-size: 9px;
  font-weight: 800;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.status-pending {
  background: #F0F1F4;
  color: #69717C;
}

.status-cancelling {
  background: #FFF4DC;
  color: #A87813;
}

.status-completed {
  background: #EDF9F3;
  color: #18845F;
}

.status-failed {
  background: #FFF0EE;
  color: #C94732;
}

.status-cancelled {
  background: #F0F1F4;
  color: #69717C;
}

.status-interrupted {
  background: #FFF4DC;
  color: #A87813;
}

.card-title-row {
  margin: 12px 0 14px;
}

.card-title-block {
  min-width: 0;
}

.card-title-block h2 {
  overflow: hidden;
  color: #171B21;
  font-size: 14px;
  font-weight: 750;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.process-id {
  overflow: hidden;
  margin-top: 4px;
  color: #9AA1AA;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stop-button {
  flex: 0 0 auto;
  padding: 7px 10px;
  border: 1px solid #F1C0B5;
  border-radius: 7px;
  background: #FFF6F3;
  color: #C34428;
  font-size: 10px;
  font-weight: 800;
  cursor: pointer;
}

.stop-button:disabled {
  opacity: .52;
  cursor: wait;
}

.resume-button {
  border-color: #B5DFD1;
  background: #F2FBF7;
  color: #167052;
}

.open-hint {
  flex: 0 0 auto;
  color: #B74D30;
  font-size: 10px;
  font-weight: 750;
}

.progress-label-row {
  align-items: baseline;
  margin-bottom: 7px;
}

.progress-message {
  overflow: hidden;
  color: #737B86;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.progress-label-row strong {
  flex: 0 0 auto;
  color: #242A32;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}

.progress-track {
  height: 6px;
  overflow: hidden;
  border-radius: 10px;
  background: #EAEDF0;
}

.progress-fill {
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #F06A43, #FF9B68);
  transition: width .45s ease;
}

.is-cancelling .progress-fill {
  background: #E8B94F;
}

.progress-fill.indeterminate {
  animation: indeterminate 1.15s ease-in-out infinite alternate;
}

.card-footer {
  margin-top: 10px;
  color: #9AA1AA;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
}

.empty-state {
  display: flex;
  min-height: 230px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  color: #858D98;
  text-align: center;
}

.empty-icon {
  display: grid;
  width: 42px;
  height: 42px;
  margin-bottom: 12px;
  place-items: center;
  border-radius: 13px;
  background: #EAF7F1;
  color: #20815F;
  font-size: 21px;
}

.loading-mark {
  width: 24px;
  height: 24px;
  margin-bottom: 13px;
  border: 2px solid #E1E5EA;
  border-top-color: #F05A35;
  border-radius: 50%;
  animation: spin .8s linear infinite;
}

.empty-state strong {
  color: #3D444E;
  font-size: 13px;
}

.empty-state p {
  margin-top: 6px;
  font-size: 11px;
}

.manager-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid #E5E8ED;
  color: #9299A3;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
}

.live-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
}

.live-indicator span {
  width: 6px;
  height: 6px;
}

@keyframes pulse {
  50% { opacity: .45; }
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@keyframes slide-in {
  from { transform: translateX(18px); opacity: .7; }
  to { transform: translateX(0); opacity: 1; }
}

@keyframes indeterminate {
  from { opacity: .55; }
  to { opacity: 1; }
}

@media (max-width: 600px) {
  .process-launcher {
    right: 14px;
    bottom: 14px;
  }

  .manager-panel {
    padding: 22px 16px 14px;
  }

  .manager-header h1 {
    font-size: 21px;
  }

  .summary-row {
    gap: 8px;
  }

  .summary-card {
    min-height: 94px;
    padding: 11px;
  }

  .refresh-button {
    width: 36px;
  }

  .card-footer {
    font-size: 8px;
  }
}
</style>
