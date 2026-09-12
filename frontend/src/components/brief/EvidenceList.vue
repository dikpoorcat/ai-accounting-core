<script setup lang="ts">
import type { EvidenceDetails } from "../../api/localKernel";

defineProps<{ items: EvidenceDetails[] }>();
</script>

<template>
  <ul class="evidence-list">
    <li v-for="item in items" :key="item.digest">
      <span class="evidence-name">{{ item.name.trim() || "原文件名未保存" }}</span>
      <details class="evidence-identity">
        <summary>查看校验标识</summary>
        <code>{{ item.digest }}</code>
      </details>
    </li>
  </ul>
</template>

<style scoped>
.evidence-list { padding-left: 20px; font-size: 13px; }
li { margin: 8px 0; overflow-wrap: anywhere; }
.evidence-name { color: var(--ink); }
.evidence-identity { margin-top: 4px; color: var(--muted); font-size: 12px; }
summary { cursor: pointer; }
code { display: block; margin-top: 6px; white-space: normal; overflow-wrap: anywhere; }
</style>
