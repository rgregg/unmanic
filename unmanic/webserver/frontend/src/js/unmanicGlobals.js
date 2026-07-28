import axios from "axios";
import { Notify, setCssVar } from 'quasar'

let $unmanic = {};

export const getUnmanicServerUrl = function () {
  if (typeof $unmanic.serverUrl === 'undefined') {
    let parser = document.createElement('a');
    parser.href = window.location.href;

    $unmanic.serverUrl = parser.protocol + '//' + parser.host;
  }
  return $unmanic.serverUrl;
}

export const getUnmanicApiUrl = function (api_version, api_endpoint) {
  if (typeof $unmanic.apiUrl === 'undefined') {
    let serverUrl = getUnmanicServerUrl();

    $unmanic.apiUrl = serverUrl + '/unmanic/api';
  }
  return $unmanic.apiUrl + '/' + api_version + '/' + api_endpoint;
}

export const setTheme = function (mode) {
  if (mode === 'dark') {
    setCssVar('primary', '#007eb4');
    setCssVar('secondary', '#0080b7');
    setCssVar('warning', '#b5902a');
    document.body.style.setProperty('--q-card-head', '#212121');
  } else {
    setCssVar('primary', '#002e5c');
    setCssVar('secondary', '#009fdd');
    setCssVar('warning', '#f2c037');
    document.body.style.setProperty('--q-card-head', '#f5f5f5');
  }
}

export default {
  $unmanic,
  getUnmanicVersion() {
    return new Promise((resolve, reject) => {
      if (typeof $unmanic.version === 'undefined') {
        axios({
          method: 'get',
          url: getUnmanicApiUrl('v2', 'version/read')
        }).then((response) => {
          $unmanic.version = response.data.version;
          resolve($unmanic.version)
        })
      } else {
        resolve($unmanic.version);
      }
    })
  },
  getUnmanicSession(options = {}) {
    return new Promise((resolve, reject) => {
      let cacheKey = 'session';
      if (options.skipProxy) {
        cacheKey = 'localSession';
      }

      if (typeof $unmanic[cacheKey] === 'undefined') {
        axios({
          method: 'get',
          url: getUnmanicApiUrl('v2', 'session/state'),
          ...options
        }).then((response) => {
          $unmanic[cacheKey] = {
            created: response.data.created,
            email: response.data.email,
            level: response.data.level,
            name: response.data.name,
            picture_uri: response.data.picture_uri,
            uuid: response.data.uuid,
          }
          resolve($unmanic[cacheKey])
        }).catch(() => {
          reject()
        })
      } else {
        resolve($unmanic[cacheKey]);
      }
    })
  },
  getUnmanicPrivacyPolicy() {
    return new Promise((resolve, reject) => {
      $unmanic.docs = (typeof $unmanic.docs === 'undefined') ? {} : $unmanic.docs
      if (typeof $unmanic.docs.privacypolicy === 'undefined') {
        axios({
          method: 'get',
          url: getUnmanicApiUrl('v2', 'docs/privacypolicy')
        }).then((response) => {
          $unmanic.docs.privacypolicy = response.data.content.join('')
          resolve($unmanic.docs.privacypolicy)
        }).catch(() => {
          reject()
        })
      } else {
        resolve($unmanic.docs.privacypolicy);
      }
    })
  },
  getUnmanicNotifications() {
    if (typeof $unmanic.notificationsList === 'undefined') {
      $unmanic.notificationsList = [];
    }
    return $unmanic.notificationsList;
  },
  updateUnmanicNotifications($t) {
    return new Promise((resolve, reject) => {
      $unmanic.notificationsList = (typeof $unmanic.notificationsList === 'undefined') ? [] : $unmanic.notificationsList
      axios({
        method: 'get',
        url: getUnmanicApiUrl('v2', 'notifications/read'),
      }).then((response) => {
        // Update success
        let notifications = []
        for (let i = 0; i < response.data.notifications.length; i++) {
          let notification = response.data.notifications[i];
          // Fetch label string from i18n
          let labelStringId = 'notifications.serverNotificationLabels.' + notification.label
          let labelString = $t(labelStringId)
          // If i18n doesn't have this string ID, then revert to just displaying the provided label
          if (labelString === labelStringId) {
            labelString = notification.label;
          }
          // Fetch message string from i18n
          let messageStringId = 'notifications.serverNotificationLabels.' + notification.message
          let messageString = $t(messageStringId)
          // If i18n doesn't have this string ID, then revert to just displaying the provided label
          if (messageString === messageStringId) {
            messageString = notification.message;
          }
          // Set the color of the notification
          let color = 'info';
          if (notification.type === 'error') {
            color = 'negative';
          } else if (notification.type === 'warning') {
            color = 'warning';
          } else if (notification.type === 'success') {
            color = 'positive';
          }
          // Add notification to list
          notifications[notifications.length] = {
            uuid: notification.uuid,
            icon: notification.icon,
            navigation: notification.navigation,
            label: labelString,
            message: messageString,
            color: color,
          }
        }
        $unmanic.notificationsList = notifications;
        resolve($unmanic.notificationsList)
      }).catch(() => {
        console.error("Failed to retrieve server notifications")
        resolve($unmanic.notificationsList)
      });
    })
  },
  dismissNotifications($t, uuidList) {
    let queryData = {
      uuid_list: uuidList
    }
    return new Promise((resolve, reject) => {
      $unmanic.notificationsList = (typeof $unmanic.notificationsList === 'undefined') ? [] : $unmanic.notificationsList
      axios({
        method: 'delete',
        url: getUnmanicApiUrl('v2', 'notifications/remove'),
        data: queryData,
      }).then((response) => {
        resolve()
      }).catch(() => {
        console.error("Failed to dismiss server notifications")
        resolve()
      });
    })
  },
}
