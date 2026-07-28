<template>
  <q-page>
    <div class="iframe-container">

      <ActionableState
        v-if="loading"
        loading
        :title="$t('components.states.loadingDataPanel')"
        :message="$t('components.states.loadingMessage')"
      />

      <ActionableState
        v-else-if="requestError"
        icon="cloud_off"
        color="negative"
        :title="$t(panelUnavailable ? 'components.states.dataPanelUnavailableTitle' : 'components.states.dataPanelsErrorTitle')"
        :message="$t(panelUnavailable ? 'components.states.dataPanelUnavailableMessage' : 'components.states.requestErrorMessage')"
        :action-label="$t('buttons.retry')"
        @action="retryPanel"
      />

      <iframe
        v-if="iframeSrc !== null"
        v-show="!loading && !requestError"
        id="data-panel-iframe"
        :src="iframeSrc"
        :data-generation="iframeGeneration"
        @load="onIframeLoad">
        {{ $t('components.states.unsupportedBrowser') }}
      </iframe>

      <ActionableState
        v-if="!loading && !requestError && iframeSrc === null"
        icon="dashboard_customize"
        :title="$t('components.states.noDataPanelsTitle')"
        :message="$t('components.dataPanels.noDataPanelsEnabled')"
        :action-label="$t('components.states.openPluginSettings')"
        @action="$router.push('/ui/settings-plugins')"
      />

    </div>
  </q-page>
</template>

<script>

import { ref } from "vue";
import axios from "axios";
import { getUnmanicApiUrl } from "src/js/unmanicGlobals";
import { LocalStorage } from "quasar";
import ActionableState from "components/ui/ActionableState.vue";

export default {
  components: { ActionableState },
  data() {
    const iframeSrc = ref(null)
    return {
      page: '',
      iframeSrc,
      iframeHeight: '0px',
      loading: true,
      requestError: false,
      panelUnavailable: false,
      intendedPanelId: null,
      requestGeneration: 0,
      iframeGeneration: 0,
    };
  },
  created() {
    console.debug('Component has been created!');
    //window.addEventListener('message', this.resizeIframe);
    if (typeof this.$route.query !== 'undefined' && typeof this.$route.query.pluginId !== 'undefined') {
      console.debug("Initial update using query in uri - " + this.$route.query.pluginId)
      this.loadPanel(this.$route.query.pluginId);
    } else {
      console.debug("Fetching enabled panel plugins list and setting the first result as current window")
      this.loadPanel();
    }
  },
  methods: {
    buildPanelUrl(pluginId) {
      const theme = LocalStorage.getItem('theme');
      return '/unmanic/panel/' + encodeURIComponent(pluginId) + '/?theme=' + encodeURIComponent(theme || '');
    },
    loadPanel(pluginId) {
      const generation = ++this.requestGeneration;
      const explicitlyRequested = typeof pluginId !== 'undefined' && pluginId !== null && pluginId !== '';
      this.intendedPanelId = explicitlyRequested ? String(pluginId) : null;
      this.loading = true;
      this.requestError = false;
      this.panelUnavailable = false;
      this.iframeSrc = null;

      return axios({
        method: 'get',
        url: getUnmanicApiUrl('v2', 'plugins/panels/enabled'),
      }).then((response) => {
        if (generation !== this.requestGeneration) {
          return;
        }
        const enabledPanels = response.data.results || [];
        const selectedPanel = explicitlyRequested
          ? enabledPanels.find((panel) => panel.plugin_id === this.intendedPanelId)
          : enabledPanels[0];

        if (!selectedPanel) {
          this.loading = false;
          if (explicitlyRequested) {
            this.panelUnavailable = true;
            this.requestError = true;
          }
          return;
        }

        const panelUrl = this.buildPanelUrl(selectedPanel.plugin_id);
        return axios.get(panelUrl, { responseType: 'text' }).then(() => {
          if (generation !== this.requestGeneration) {
            return;
          }
          this.iframeGeneration = generation;
          this.iframeSrc = panelUrl;
        });
      }).catch(() => {
        if (generation !== this.requestGeneration) {
          return;
        }
        this.loading = false;
        this.requestError = true;
        this.panelUnavailable = false;
        this.$q.notify({
          color: 'negative',
          position: 'top',
          message: this.$t('notifications.failedToLoadDataPanel'),
          icon: 'report_problem',
          actions: [{ icon: 'close', color: 'white' }]
        })
      })
    },
    retryPanel() {
      return this.loadPanel(this.intendedPanelId === null ? undefined : this.intendedPanelId);
    },
    onIframeLoad(event) {
      const generation = Number(event.currentTarget.dataset.generation);
      if (generation === this.requestGeneration && generation === this.iframeGeneration) {
        this.loading = false;
      }
    }
  },
  watch: {
    $route(to, from) {
      if (typeof to.query !== 'undefined' && typeof to.query.pluginId !== 'undefined') {
        console.debug("Detected change in route with query in uri - " + to.query.pluginId)
        this.loadPanel(to.query.pluginId);
      } else {
        this.loadPanel();
      }
    }
  }
}
</script>

<style>
.iframe-container {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  margin: 20px;
}

.iframe-container iframe {
  display: block;
  width: 100%;
  height: 100%;
  border: none;
}
</style>
