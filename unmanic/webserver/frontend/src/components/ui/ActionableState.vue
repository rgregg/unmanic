<template>
  <div
    class="actionable-state full-width column items-center justify-center text-center q-pa-lg"
    :aria-busy="loading"
  >
    <div
      class="column items-center"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <q-spinner
        v-if="loading"
        aria-hidden="true"
        color="secondary"
        size="3em"
      />
      <q-icon
        v-else
        aria-hidden="true"
        :name="icon"
        :color="color"
        size="3em"
      />
      <div class="text-subtitle1 text-weight-medium q-mt-md">{{ title }}</div>
      <div v-if="message" class="text-body2 text-grey-7 q-mt-xs">{{ message }}</div>
    </div>
    <q-btn
      v-if="!loading && actionLabel"
      class="q-mt-md"
      outline
      color="secondary"
      :label="actionLabel"
      @click="$emit('action')"
    />
  </div>
</template>

<script setup>
defineProps({
  loading: {
    type: Boolean,
    default: false
  },
  icon: {
    type: String,
    default: 'info'
  },
  color: {
    type: String,
    default: 'grey-7'
  },
  title: {
    type: String,
    required: true
  },
  message: {
    type: String,
    default: ''
  },
  actionLabel: {
    type: String,
    default: ''
  }
})

defineEmits(['action'])
</script>

<style scoped>
.actionable-state {
  min-height: 180px;
}

@media (max-width: 1023px) {
  .actionable-state {
    width: 100vw;
    max-width: 100%;
    min-height: 150px;
    padding: 24px 16px;
  }
}
</style>
