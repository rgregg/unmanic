<template>
  <q-page padding class="page-with-mobile-quick-nav">
    <!-- content -->

    <div class="q-pa-none">

      <div class="row">
        <div class="col-sm-12 col-md-10 col-lg-8">
          <div :class="$q.platform.is.mobile ? 'q-ma-sm' : 'q-ma-sm q-pa-md'">

            <q-form
              @submit="save"
              class="q-gutter-md"
            >
              <AdmonitionBanner
                v-if="isDirty"
                type="warning"
                :title="$t('components.settings.common.unsavedChanges')">
                {{ $t('components.settings.common.unsavedChangesBody') }}
              </AdmonitionBanner>

              <!--START THIS INSTALLATION-->
              <h5 class="q-mb-none">{{ $t('components.settings.link.thisInstallation') }}</h5>
              <div class="q-gutter-sm">
                <q-skeleton
                  v-if="installationName === null"
                  type="QInput"/>
                <q-input
                  v-if="installationName !== null"
                  outlined
                  color="primary"
                  v-model="installationName"
                  :label="$t('components.settings.link.nameThisInstall')"
                  :placeholder="installationName">
                </q-input>
                <q-skeleton
                  v-if="installationPublicAddress === null"
                  type="QInput"/>
                <q-input
                  v-if="installationPublicAddress !== null"
                  outlined
                  color="primary"
                  v-model="installationPublicAddress"
                  :label="$t('components.settings.link.installationPublicAddress')"
                  :placeholder="installationPublicAddress"
                  :rules="[
                    val => validatePublicAddress(val) || $t('components.settings.link.addressProtocolValidation')
                  ]"
                >
                </q-input>
              </div>

              <AdmonitionBanner type="note" class="q-mt-md">
                <p>{{ $t('components.settings.link.syncTipBody') }}</p>
              </AdmonitionBanner>
              <!--END THIS INSTALLATION-->

              <q-separator class="q-my-lg"/>

              <!--START REMOTE INSTALLATIONS-->
              <h5 class="q-mb-none">{{ $t('components.settings.link.remoteInstallations') }}</h5>
              <div class="q-gutter-sm">
                <q-skeleton
                  v-if="remoteInstallations === null"
                  type="text"/>

                <q-list
                  bordered
                  separator
                  class="rounded-borders">

                  <q-item
                    v-for="(installation, index) in remoteInstallations"
                    v-bind:key="index">

                    <q-item-section avatar>
                      <q-img
                        v-if="installation.available"
                        src="~assets/trawlarr-logo-white.png"/>
                      <q-img
                        v-else
                        :img-style="{ filter: 'grayscale(100%)' }"
                        src="~assets/trawlarr-logo-white.png"/>
                      <q-tooltip v-if="installation.available">
                        {{ $t('tooltips.available') }}
                      </q-tooltip>
                      <q-tooltip v-else>
                        {{ $t('tooltips.unavailable') }}
                      </q-tooltip>
                    </q-item-section>

                    <q-separator inset vertical class="q-mr-sm"/>

                    <q-item-section class="q-px-sm q-mx-sm">
                      <!--Link Address-->
                      <q-item-label lines="1">
                        {{ installation.address }}
                      </q-item-label>

                      <!--Link Name-->
                      <q-item-label caption lines="2">
                        <div class="row q-mt-sm">
                          <div class="col-3">
                            <span class="text-weight-bold">{{ $t('components.settings.link.name') }}:</span>
                          </div>
                          <div class="col-grow">
                            <span class="q-pl-none">{{ installation.name }}</span>
                          </div>
                        </div>
                      </q-item-label>

                      <!--Link Version-->
                      <q-item-label caption lines="2">
                        <div class="row">
                          <div class="col-3">
                            <span class="text-weight-bold">{{ $t('components.settings.link.version') }}:</span>
                          </div>
                          <div class="col-grow">
                            <span class="q-pl-none">{{ installation.version }}</span>
                          </div>
                        </div>
                      </q-item-label>
                    </q-item-section>

                    <q-item-section v-if="!$q.platform.is.mobile">
                      <q-item-label lines="1">
                        <div class="row">
                          <div class="col-6 text-left">
                            <span :class="installation.enableReceivingTasks ? 'text-primary' : 'text-grey-8'">
                              <q-icon v-if="installation.enableReceivingTasks" color="check" name="check"/>
                              <q-icon v-else color="close" name="close"/>
                              |
                              {{ $t('components.settings.link.linkReceivingTasksStatusLabel') }}
                            </span>
                          </div>
                        </div>
                      </q-item-label>
                      <q-item-label lines="1">
                        <div class="row">
                          <div class="col-6 text-left">
                            <span :class="installation.enableSendingTasks ? 'text-primary' : 'text-grey-8'">
                              <q-icon v-if="installation.enableSendingTasks" color="check" name="check"/>
                              <q-icon v-else color="close" name="close"/>
                              |
                              {{ $t('components.settings.link.linkSendingTasksStatusLabel') }}
                            </span>
                          </div>
                        </div>
                      </q-item-label>
                      <q-item-label v-if="installation.enableSendingTasks" lines="1">
                        <div class="row">
                          <div class="col-6 text-left">
                            <span :class="installation.enableTaskPreloading ? 'text-primary' : 'text-grey-8'">
                              <q-icon v-if="installation.enableTaskPreloading" color="check" name="check"/>
                              <q-icon v-else color="close" name="close"/>
                              |
                              {{ $t('components.settings.link.linkPreloadRemoteTasksStatusLabel') }}
                            </span>
                          </div>
                        </div>
                      </q-item-label>
                      <q-item-label v-if="installation.enableSendingTasks" lines="1">
                        <div class="row">
                          <div class="col-6 text-left">
                            <span :class="installation.enableChecksumValidation ? 'text-primary' : 'text-grey-8'">
                              <q-icon v-if="installation.enableChecksumValidation" color="check" name="check"/>
                              <q-icon v-else color="close" name="close"/>
                              |
                              {{ $t('components.settings.link.linkConfigChecksumValidationStatusLabel') }}
                            </span>
                          </div>
                        </div>
                      </q-item-label>
                      <q-item-label v-if="installation.enableSendingTasks" lines="1">
                        <div class="row">
                          <div class="col-6 text-left">
                            <span :class="installation.enableConfigMissingLibraries ? 'text-primary' : 'text-grey-8'">
                              <q-icon v-if="installation.enableConfigMissingLibraries" color="check" name="check"/>
                              <q-icon v-else color="close" name="close"/>
                              |
                              {{ $t('components.settings.link.linkConfigRemoteLibrariesStatusLabel') }}
                            </span>
                          </div>
                        </div>
                      </q-item-label>
                      <q-item-label lines="1">
                        <div class="row">
                          <div class="col-6 text-left">
                            <span :class="installation.enableDistributedWorkers ? 'text-primary' : 'text-grey-8'">
                              <q-icon v-if="installation.enableDistributedWorkers" color="check" name="check"/>
                              <q-icon v-else color="close" name="close"/>
                              |
                              {{ $t('components.settings.link.linkDistributedWorkersStatusLabel') }}
                            </span>
                          </div>
                        </div>
                      </q-item-label>
                    </q-item-section>

                    <q-separator inset vertical class="q-mx-sm"/>

                    <q-item-section center side>
                      <div class="text-grey-8 q-gutter-xs">
                        <UnmanicListActionButton
                          icon="tune"
                          color="grey-8"
                          :tooltip="$t('tooltips.configure')"
                          :aria-label="$t('components.settings.link.configureNamedInstallation', {
                            installation: installation.name || installation.address ||
                              $t('components.settings.link.unnamedInstallation')
                          })"
                          @click="configureRemoteInstallation(index)"
                        />
                        <UnmanicListActionButton
                          icon="delete"
                          color="negative"
                          :tooltip="$t('tooltips.delete')"
                          :aria-label="$t('components.settings.link.deleteNamedInstallation', {
                            installation: installation.name || installation.address ||
                              $t('components.settings.link.unnamedInstallation')
                          })"
                          @click="deleteRemoteInstallation(index)"
                        />
                      </div>
                    </q-item-section>

                  </q-item>

                </q-list>

                <q-bar class="bg-transparent">
                  <q-space/>
                  <UnmanicListAddButton
                    :tooltip="$t('tooltips.add')"
                    @click="openNewRemoteInstallationDialog"
                  />
                </q-bar>

                <UnmanicDialogPopup
                  ref="addRemoteDialogRef"
                  :title="$t('components.settings.link.addRemoteInstallation')"
                  :mini="true"
                  :persistent="isAddRemoteDirty || addingRemote"
                  :close-disabled="addingRemote"
                  :close-tooltip="isAddRemoteDirty ? $t('components.settings.common.closeWithoutSaving') : ''"
                  :actions="addRemoteDialogActions"
                  @add="addNewRemoteInstallation"
                  @hide="onAddRemoteDialogHide"
                >
                  <div class="q-pa-md">
                    <q-input
                      outlined
                      color="primary"
                      v-model="newRemoteInstallationAddress"
                      :label="$t('components.settings.link.address')"
                      placeholder="192.168.1.2:8888"
                      :rules="[
                        val => validateRemoteAddress(val) || $t('components.settings.link.addressRequired')
                      ]"
                      class="q-mb-md"
                    />

                    <q-select
                      outlined
                      v-model="newRemoteInstallationAuthenticationType"
                      :options="newRemoteInstallationAuthenticationOptions"
                      :label="$t('components.settings.link.authentication')"
                      class="q-mb-md"
                    />

                    <template v-if="newRemoteInstallationAuthenticationType !== 'None'">
                      <q-input
                        outlined
                        color="primary"
                        v-model="newRemoteInstallationUsername"
                        :label="$t('components.settings.link.username')"
                        class="q-mb-md"
                      />

                      <q-input
                        outlined
                        color="primary"
                        v-model="newRemoteInstallationPassword"
                        :label="$t('components.settings.link.password')"
                      />
                    </template>
                  </div>
                </UnmanicDialogPopup>

              </div>
              <!--END REMOTE INSTALLATIONS-->

              <q-separator class="q-my-lg"/>

              <div>
                <UnmanicSettingsSubmitButton
                  :disable="!isDirty || !isFormValid || saving"
                  :loading="saving"
                />
              </div>
            </q-form>

          </div>
        </div>
      </div>

      <RemoteInstallLinkDialog
        ref="remoteInstallDialogRef"
        :uuid="activeRemoteInstallationUuid"
        @saved="onRemoteInstallSaved"
        @hide="onRemoteInstallHide"
      />

      <MobileSettingsQuickNav
        v-bind:prevEnabled="true"
        v-bind:prevLabel="$t('navigation.plugins')"
        v-bind:prevPath="'/ui/settings-plugins'"
        v-bind:nextEnabled="false"
        v-bind:nextLabel="'none'"
        v-bind:nextPath="'/ui/settings-plugins'"/>

    </div>
  </q-page>
</template>

<script>
import { UnmanicWebsocketHandler } from "src/js/unmanicWebsocket";
import { onMounted, onUnmounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import axios from "axios";
import { getUnmanicApiUrl } from "src/js/unmanicGlobals";
import RemoteInstallLinkDialog from "components/settings/link/RemoteInstallLinkDialog.vue";
import MobileSettingsQuickNav from "components/MobileSettingsQuickNav";
import AdmonitionBanner from "components/ui/AdmonitionBanner.vue";
import UnmanicSettingsSubmitButton from "components/ui/buttons/UnmanicSettingsSubmitButton.vue";
import UnmanicListActionButton from "components/ui/buttons/UnmanicListActionButton.vue";
import UnmanicListAddButton from "components/ui/buttons/UnmanicListAddButton.vue";
import UnmanicDialogPopup from "components/ui/dialogs/UnmanicDialogPopup.vue";

export default {
  name: 'SettingsLink',
  components: {
    RemoteInstallLinkDialog,
    MobileSettingsQuickNav,
    AdmonitionBanner,
    UnmanicSettingsSubmitButton,
    UnmanicListActionButton,
    UnmanicListAddButton,
    UnmanicDialogPopup
  },
  setup() {
    const { t: $t } = useI18n();

    const addRemoteDialogRef = ref(null);

    /**
     * Unmanic WS handle
     * @type {null}
     */
    let ws = null;
    let unmanicWSHandler = UnmanicWebsocketHandler($t);

    function initUnmanicWebsocket() {
      ws = unmanicWSHandler.init();
    }

    function closeUnmanicWebsocket() {
      unmanicWSHandler.close();
    }

    // END UNMANIC WS HANDLE

    onMounted(() => {
      // Start the websocket
      initUnmanicWebsocket();
    })
    onUnmounted(() => {
      // Close the websocket
      closeUnmanicWebsocket();
    })

    return {
      addRemoteDialogRef
    }
  },
  data() {
    return {
      installationName: ref(null),
      installationPublicAddress: ref(null),
      remoteInstallations: ref(null),
      newRemoteInstallation: ref(false),
      newRemoteInstallationAddress: ref(''),
      newRemoteInstallationAuthenticationType: ref('None'),
      newRemoteInstallationAuthenticationOptions: ref(['None', 'Basic']),
      newRemoteInstallationUsername: ref(null),
      newRemoteInstallationPassword: ref(null),
      activeRemoteInstallationUuid: ref(''),
      initialSnapshot: ref(null),
      saving: ref(false),
      addRemoteInitialSnapshot: ref(null),
      addingRemote: ref(false),
    }
  },
  computed: {
    settingsSnapshot() {
      if (this.installationName === null || this.installationPublicAddress === null) {
        return null
      }
      return JSON.stringify({
        installationName: this.installationName,
        installationPublicAddress: this.installationPublicAddress,
      })
    },
    isDirty() {
      return this.initialSnapshot !== null && this.settingsSnapshot !== null &&
        this.initialSnapshot !== this.settingsSnapshot
    },
    isFormValid() {
      return this.settingsSnapshot !== null && this.validatePublicAddress(this.installationPublicAddress)
    },
    addRemoteSnapshot() {
      return JSON.stringify({
        address: this.newRemoteInstallationAddress,
        authenticationType: this.newRemoteInstallationAuthenticationType,
        username: this.newRemoteInstallationUsername,
        password: this.newRemoteInstallationPassword,
      })
    },
    isAddRemoteDirty() {
      return this.addRemoteInitialSnapshot !== null &&
        this.addRemoteInitialSnapshot !== this.addRemoteSnapshot
    },
    isAddRemoteValid() {
      return this.validateRemoteAddress(this.newRemoteInstallationAddress)
    },
    addRemoteDialogActions() {
      return [
        {
          label: this.$t('components.settings.link.add'),
          icon: 'add',
          color: this.isAddRemoteDirty && this.isAddRemoteValid ? 'positive' : 'grey-6',
          emit: 'add',
          disabled: !this.isAddRemoteDirty || !this.isAddRemoteValid || this.addingRemote,
        }
      ]
    },
    dragOptions() {
      return {
        animation: 100,
        group: "scheduleOrder",
        disabled: false,
        ghostClass: "ghost",
        direction: "vertical",
        delay: 200,
        delayOnTouchOnly: true,
      };
    }
  },
  methods: {
    updateSnapshot: function () {
      this.initialSnapshot = this.settingsSnapshot
    },
    updateAddRemoteSnapshot: function () {
      this.addRemoteInitialSnapshot = this.addRemoteSnapshot
    },
    handleBeforeUnload: function (event) {
      if (!this.isDirty && !this.saving && !this.isAddRemoteDirty && !this.addingRemote) {
        return
      }
      event.preventDefault()
      event.returnValue = ''
    },
    validatePublicAddress(val) {
      if (!val) return true
      return val.toLowerCase().startsWith('http')
    },
    validateRemoteAddress(val) {
      return typeof val === 'string' && val.trim().length > 0
    },
    fetchSettings: function () {
      // Fetch current settings
      axios({
        method: 'get',
        url: getUnmanicApiUrl('v2', 'settings/read')
      }).then((response) => {
        // Set the installation name
        const preserveStagedSettings = this.isDirty
        if (!preserveStagedSettings) {
          this.installationName = response.data.settings.installation_name
          this.installationPublicAddress = response.data.settings.installation_public_address
        }
        // Set the list of remote installations
        let remoteInstallationsList = []
        for (let i = 0; i < response.data.settings.remote_installations.length; i++) {
          let remoteInstallation = response.data.settings.remote_installations[i];
          remoteInstallationsList[remoteInstallationsList.length] = {
            address: remoteInstallation.address,
            enableReceivingTasks: remoteInstallation.enable_receiving_tasks,
            enableSendingTasks: remoteInstallation.enable_sending_tasks,
            enableTaskPreloading: remoteInstallation.enable_task_preloading,
            enableChecksumValidation: remoteInstallation.enable_checksum_validation,
            enableConfigMissingLibraries: remoteInstallation.enable_config_missing_libraries,
            enableDistributedWorkers: remoteInstallation.enable_distributed_worker_count,
            name: remoteInstallation.name,
            version: remoteInstallation.version,
            uuid: remoteInstallation.uuid,
            available: remoteInstallation.available,
          }
        }
        this.remoteInstallations = remoteInstallationsList;
        if (!preserveStagedSettings) {
          this.updateSnapshot()
        }
      }).catch(() => {
        this.$q.notify({
          color: 'negative',
          position: 'top',
          message: this.$t('notifications.failedToFetchSettings'),
          icon: 'report_problem',
          actions: [{ icon: 'close', color: 'white' }]
        })
      });
    },
    save: async function () {
      if (this.saving || !this.isDirty || !this.isFormValid) {
        if (!this.isFormValid) {
          this.$q.notify({
            color: 'negative',
            position: 'top',
            message: this.$t('components.settings.link.invalidPublicAddress'),
            icon: 'report_problem'
          })
        }
        return
      }
      this.saving = true
      const submittedSnapshot = this.settingsSnapshot

      // Save settings
      let remoteInstallationsList = []
      for (let i = 0; i < this.remoteInstallations.length; i++) {
        remoteInstallationsList[remoteInstallationsList.length] = {
          address: this.remoteInstallations[i].address,
          enable_receiving_tasks: this.remoteInstallations[i].enableReceivingTasks,
          enable_sending_tasks: this.remoteInstallations[i].enableSendingTasks,
          name: this.remoteInstallations[i].name,
          version: this.remoteInstallations[i].version,
          uuid: this.remoteInstallations[i].uuid,
          available: this.remoteInstallations[i].available,
        };
      }
      let data = {
        settings: {
          installation_name: this.installationName,
          installation_public_address: this.installationPublicAddress,
        }
      }
      try {
        await axios({
        method: 'post',
        url: getUnmanicApiUrl('v2', 'settings/write'),
        data: data
        })
        this.initialSnapshot = submittedSnapshot
        this.$q.notify({
          color: 'positive',
          position: 'top',
          icon: 'cloud_done',
          message: this.$t('notifications.saved'),
          timeout: 200
        })

        // Force reload of session to register the name change
        axios({
          method: 'post',
          url: getUnmanicApiUrl('v2', 'session/reload'),
        }).catch(() => {
          // Ignore error
        })
      } catch (error) {
        this.$q.notify({
          color: 'negative',
          position: 'top',
          message: this.$t('notifications.failedToSaveSettings'),
          icon: 'report_problem',
          actions: [{ icon: 'close', color: 'white' }]
        })
      } finally {
        this.saving = false
      }
    },
    addNewRemoteInstallation: async function () {
      if (this.addingRemote || !this.isAddRemoteDirty || !this.isAddRemoteValid) {
        return
      }
      // Ensure this remote installation is not already in the list
      for (let i = 0; i < this.remoteInstallations.length; i++) {
        if (this.newRemoteInstallationAddress === this.remoteInstallations[i].address) {
          this.$q.notify({
            color: 'negative',
            position: 'top',
            message: this.$t('notifications.remoteInstallationAlreadyInList'),
            icon: 'report_problem',
            actions: [{ icon: 'close', color: 'white' }]
          })
          return;
        }
      }
      this.addingRemote = true

      const submitted = {
        address: this.newRemoteInstallationAddress,
        authenticationType: this.newRemoteInstallationAuthenticationType,
        username: this.newRemoteInstallationUsername,
        password: this.newRemoteInstallationPassword,
      }
      const submittedSnapshot = JSON.stringify(submitted)
      const validationData = {
        address: submitted.address,
        auth: submitted.authenticationType,
        username: submitted.username,
        password: submitted.password,
      }
      let connectionValidated = false
      try {
        const response = await axios({
          method: 'post',
          url: getUnmanicApiUrl('v2', 'settings/link/validate'),
          data: validationData
        })
        connectionValidated = true
        // Ensure this remote installation was compatible with linking
        if (typeof response.data.installation.settings.installation_name === 'undefined') {
          this.$q.notify({
            color: 'negative',
            position: 'top',
            message: this.$t('notifications.incompatibleRemoteInstallation'),
            icon: 'report_problem',
            actions: [{ icon: 'close', color: 'white' }]
          })
          return
        } else {
          // Get name and version from API validation
          let name = response.data.installation.settings.installation_name;
          let version = response.data.installation.version;
          let uuid = response.data.installation.session.uuid;
          // Trigger a save event
          let data = {
            link_config: {
              uuid: uuid,
              name: name,
              version: version,
              available: true,
              address: submitted.address,
              auth: submitted.authenticationType,
              username: submitted.username,
              password: submitted.password,
              enable_receiving_tasks: false,
              enable_sending_tasks: false,
            },
          }
          await axios({
            method: 'post',
            url: getUnmanicApiUrl('v2', 'settings/link/write'),
            data: data
          })
          this.remoteInstallations.push({
            address: submitted.address,
            auth: submitted.authenticationType,
            username: submitted.username,
            password: submitted.password,
            enableReceivingTasks: false,
            enableSendingTasks: false,
            enableTaskPreloading: false,
            enableChecksumValidation: false,
            enableConfigMissingLibraries: false,
            enableDistributedWorkers: false,
            name,
            version,
            uuid,
            available: true,
          })
          this.addRemoteInitialSnapshot = submittedSnapshot
          this.$q.notify({
            color: 'positive',
            position: 'top',
            icon: 'cloud_done',
            message: this.$t('notifications.saved'),
            timeout: 200
          })
          if (this.addRemoteSnapshot === submittedSnapshot) {
            this.addingRemote = false
            await this.$nextTick()
            this.addRemoteDialogRef.hide()
          }
        }
      } catch (error) {
        this.$q.notify({
          color: 'negative',
          position: 'top',
          message: this.$t(connectionValidated
            ? 'notifications.failedToSaveSettings'
            : 'notifications.invalidRemoteInstallationAddress'),
          icon: 'report_problem',
          actions: [{ icon: 'close', color: 'white' }]
        })
      } finally {
        this.addingRemote = false
      }
    },
    deleteRemoteInstallation: function (index) {
      let newList = []
      for (let i = 0; i < this.remoteInstallations.length; i++) {
        if (i === index) {
          // Request a DELETE from server
          let data = {
            uuid: this.remoteInstallations[i].uuid,
          }
          axios({
            method: 'delete',
            url: getUnmanicApiUrl('v2', 'settings/link/remove'),
            data: data
          }).then((response) => {
            // Save success, show feedback
            this.$q.notify({
              color: 'positive',
              position: 'top',
              icon: 'cloud_done',
              message: this.$t('notifications.saved'),
              timeout: 200
            })
            // Update list
            //this.fetchLibraryList();
          }).catch(() => {
            this.$q.notify({
              color: 'negative',
              position: 'top',
              message: this.$t('notifications.failedToSaveSettings'),
              icon: 'report_problem',
              actions: [{ icon: 'close', color: 'white' }]
            })
          });
          // Remove item from the list by skipping it this loop
          continue;
        }
        newList[newList.length] = this.remoteInstallations[i];
      }
      this.remoteInstallations = newList;
    },
    configureRemoteInstallation: function (index) {
      let installation = this.remoteInstallations[index]
      this.activeRemoteInstallationUuid = installation.uuid
      this.$nextTick(() => {
        if (this.$refs.remoteInstallDialogRef) {
          this.$refs.remoteInstallDialogRef.show()
        }
      })
    },
    openNewRemoteInstallationDialog: function () {
      this.newRemoteInstallationAddress = ''
      this.newRemoteInstallationAuthenticationType = 'None'
      this.newRemoteInstallationUsername = null
      this.newRemoteInstallationPassword = null
      this.updateAddRemoteSnapshot()
      this.addRemoteDialogRef.show()
    },
    onAddRemoteDialogHide: function () {
      if (this.addingRemote) {
        return
      }
      this.newRemoteInstallationAddress = ''
      this.newRemoteInstallationAuthenticationType = 'None'
      this.newRemoteInstallationUsername = null
      this.newRemoteInstallationPassword = null
      this.updateAddRemoteSnapshot()
    },
    onRemoteInstallSaved: function () {
      this.fetchSettings()
    },
    onRemoteInstallHide: function () {
      this.activeRemoteInstallationUuid = ''
    }
  },
  created() {
    this.fetchSettings();
  },
  mounted() {
    window.addEventListener('beforeunload', this.handleBeforeUnload)
  },
  beforeUnmount() {
    window.removeEventListener('beforeunload', this.handleBeforeUnload)
  },
  beforeRouteLeave(to, from, next) {
    const hasPendingChanges = this.isDirty || this.saving || this.isAddRemoteDirty || this.addingRemote
    if (!hasPendingChanges || window.confirm(this.$t('components.settings.common.leaveWithUnsavedChanges'))) {
      next()
      return
    }
    next(false)
  }
}
</script>
<style>
.ghost {
  opacity: 0;
}

div.sub-setting {
  margin-left: 30px;
  padding-top: 8px;
  padding-left: 8px;
  border-left: solid thin var(--q-primary);
}
</style>
