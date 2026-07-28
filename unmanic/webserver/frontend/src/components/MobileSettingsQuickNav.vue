<template>
  <!--START QUICK NAV-->
  <div class="mobile-quick-nav lt-md">
    <div ref="quickNavContent">
      <q-separator/>
      <div class="row q-pa-sm">
        <div class="col-6 q-pr-xs">
          <UnmanicStandardButton
            v-if="prevEnabled"
            icon="navigate_before"
            class="full-width"
            @click="$router.push(prevPath)"
            :label="prevLabel"
          />
        </div>
        <div class="col-6 q-pl-xs">
          <UnmanicStandardButton
            v-if="nextEnabled"
            icon-right="navigate_next"
            class="full-width"
            @click="$router.push(nextPath)"
            :label="nextLabel"
          />
        </div>
      </div>
    </div>
  </div>
  <!--END QUICK NAV-->
</template>

<script>
import UnmanicStandardButton from "components/ui/buttons/UnmanicStandardButton.vue";
import { onMounted, onUnmounted, ref } from "vue";

export default {
  name: 'MobileSettingsQuickNav',
  components: { UnmanicStandardButton },
  props: {
    prevEnabled: {
      type: Boolean
    },
    prevLabel: {
      type: String
    },
    prevPath: {
      type: String
    },
    nextEnabled: {
      type: Boolean
    },
    nextLabel: {
      type: String
    },
    nextPath: {
      type: String
    }
  },
  setup() {
    const quickNavContent = ref(null);
    let resizeObserver = null;

    onMounted(() => {
      resizeObserver = new ResizeObserver(([entry]) => {
        const navHeight = Math.ceil(entry.target.getBoundingClientRect().height);
        if (navHeight > 0) {
          document.documentElement.style.setProperty(
            '--mobile-settings-quick-nav-height',
            `${navHeight}px`
          );
        }
      });
      resizeObserver.observe(quickNavContent.value);
    });

    onUnmounted(() => {
      resizeObserver?.disconnect();
      document.documentElement.style.removeProperty('--mobile-settings-quick-nav-height');
    });

    return { quickNavContent }
  }
}
</script>

<style scoped>
.mobile-quick-nav {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 100;
  background: var(--q-card-head);
  min-height: calc(
    var(--mobile-settings-quick-nav-height) + env(safe-area-inset-bottom, 0px)
  );
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
</style>
