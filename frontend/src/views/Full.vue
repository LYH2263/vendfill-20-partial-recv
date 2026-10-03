<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const lanes = ref<any[]>([])
const error = ref('')
onMounted(async () => {
  try {
    lanes.value = (await api('/refills/full?location_id=1')).lanes
  } catch (e: any) {
    try { error.value = JSON.parse(e.message).detail ?? e.message } catch { error.value = e.message }
  }
})
</script>
<template>
  <h1>满仓</h1>
  <p class="sub">缺口为 0 的货道（无需补货）</p>
  <div v-if="error" class="card" style="border-color:var(--vf-amber);color:var(--vf-amber);font-size:0.85rem">
    {{ error }}
  </div>
  <div v-else class="card">
    <table>
      <thead><tr><th>货道</th><th>商品</th><th>库存</th><th>在途</th><th>容量</th></tr></thead>
      <tbody>
        <tr v-for="l in lanes" :key="l.lane_id">
          <td>{{ l.slot_no }}</td><td>{{ l.sku_name }}</td><td>{{ l.stock }}</td><td>{{ l.in_transit }}</td><td>{{ l.capacity }}</td>
        </tr>
        <tr v-if="!lanes.length"><td colspan="5" class="muted">暂无满仓货道</td></tr>
      </tbody>
    </table>
  </div>
</template>
