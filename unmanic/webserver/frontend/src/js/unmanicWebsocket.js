import { Notify } from 'quasar'
import $unmanic from './unmanicGlobals'

/**
 * Function for handle default WS connection to the Unmanic service.
 * This will return a WS instance which can be expanded upon with
 * additional requests depending on the page's requirements.
 *
 * @param $t
 * @returns {{init: (function(): *), close: close}}
 * @constructor
 */
export const UnmanicWebsocketHandler = function ($t) {
  const owner = Symbol('websocket-handler');
  const manager = $unmanic.websocketManager || {
    listeners: new Map(),
    owners: new Set(),
    generation: 0,
    reconnectTimer: null,
    manualReconnectTimer: null,
    warningTimer: null,
    warningInterval: null,
    clearConnectionWarning: null,
    intentionalClose: false,
    serverId: null,
    baseListenersRegistered: false,
  };
  $unmanic.websocketManager = manager;

  function attachListener(socket, definition) {
    if (definition.sockets.has(socket)) {
      return;
    }
    const guardedCallback = (event) => {
      if (socket.__unmanicGeneration === manager.generation) {
        definition.callback(event);
      }
    };
    socket.addEventListener(definition.type, guardedCallback);
    definition.sockets.add(socket);
    definition.wrappers.set(socket, guardedCallback);
  }

  function registerListener(type, key, callback, listenerOwner = owner) {
    if (!manager.listeners.has(key)) {
      manager.listeners.set(key, {
        type,
        callback,
        owner: listenerOwner,
        sockets: new WeakSet(),
        wrappers: new WeakMap(),
      });
    }
    const definition = manager.listeners.get(key);
    if ($unmanic.ws) {
      attachListener($unmanic.ws, definition);
    }
  }

  function openWS() {
    if (manager.owners.size === 0) {
      return null;
    }
    if (typeof $unmanic.ws !== 'undefined' && $unmanic.ws !== null) {
      return $unmanic.ws;
    }

    let loc = window.location;
    let newUri = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    newUri += '//' + loc.host + '/unmanic/websocket';

    const target = localStorage.getItem('unmanic-installation-target');
    if (target && target !== 'local') {
      newUri += '?target_id=' + encodeURIComponent(target);
    }

    const socket = new WebSocket(newUri);
    manager.generation += 1;
    socket.__unmanicGeneration = manager.generation;
    $unmanic.ws = socket;
    manager.listeners.forEach((definition) => attachListener(socket, definition));
    return socket;
  }

  function scheduleReconnect() {
    clearTimeout(manager.reconnectTimer);
    manager.reconnectTimer = setTimeout(() => {
      manager.reconnectTimer = null;
      if (manager.owners.size === 0) {
        return;
      }
      console.debug('Attempting reconnect to Unmanic server...');
      openWS();
    }, 4000);
  }

  function dismissConnectionWarning() {
    clearTimeout(manager.warningTimer);
    clearInterval(manager.warningInterval);
    manager.warningTimer = null;
    manager.warningInterval = null;
    if (manager.clearConnectionWarning) {
      manager.clearConnectionWarning();
      manager.clearConnectionWarning = null;
    }
  }

  /**
   * Init the websocket to the unmanic backend server
   *
   * @returns {null|WebSocket|*}
   */
  const initWebsocket = function () {

    function showWebsocketConnectionWarning() {
      if (manager.owners.size === 0) {
        return;
      }
      // Ensure the websocket is actually missing
      if (typeof $unmanic.ws !== 'undefined' && $unmanic.ws !== null) {
        return;
      }
      if (manager.clearConnectionWarning === null) {
        console.debug("Display websocket disconnect warning")
        manager.clearConnectionWarning = Notify.create({
          timeout: 0,
          spinner: true,
          color: 'warning',
          position: 'top',
          message: $t('notifications.backendConnectionWarning'),
          icon: 'report_problem'
        });
        manager.warningInterval = setInterval(() => {
          if (manager.owners.size === 0) {
            dismissConnectionWarning();
            return;
          }
          if (typeof $unmanic.ws !== 'undefined' && $unmanic.ws !== null) {
            if ($unmanic.ws.readyState === WebSocket.OPEN) {
              console.log("Websocket has reconnected. Clearing warning.")
              dismissConnectionWarning();
            }
          }
        }, 500);
      }
    }

    function dismissMessages(message_id) {
      if (typeof $unmanic.frontendMessage === 'undefined') {
        return
      }
      if (typeof $unmanic.frontendMessage[message_id] === 'function') {
        $unmanic.frontendMessage[message_id]();
        if (typeof $unmanic.ws !== 'undefined' && $unmanic.ws !== null) {
          $unmanic.ws.send(JSON.stringify({ command: 'dismiss_message', params: { message_id: message_id } }));
        }
      }
      if (typeof $unmanic.frontendMessage[message_id] !== 'undefined') {
        delete $unmanic.frontendMessage[message_id]
      }
    }

    function displayStatus($t, message_id, type, code, message, timeout) {
      // Create new status message
      // Fetch message string from i18n
      let notificationStringId = 'notifications.serverMessages.' + code
      let notificationString = $t(notificationStringId)
      // If i18n doesnt have this string ID, then revert to default
      if (notificationString === notificationStringId) {
        notificationString = $t('notifications.serverMessages.defaults.' + type);
      }
      // If the message is not empty, concatenate it to the end of the notification string
      if (message) {
        notificationString = notificationString + '<br>' + message;
      } else {
        // Check if a preset message is available
        let messageStringId = 'notifications.serverMessages.' + code + 'Message'
        let messageString = $t(messageStringId)
        // If i18n doesnt have this string ID, then revert to default
        if (messageString !== messageStringId) {
          message = $t('notifications.serverMessages.' + code + 'Message');
          // Concatenate it to the end
          notificationString = notificationString + '<br>' + message;
        }
      }

      notificationString = '' +
        '<span style="display:block;min-height:50px;white-space:pre;">' +
        notificationString +
        '</span>'

      let icon = 'announcement';

      if (!(message_id in $unmanic.frontendMessage)) {
        $unmanic.frontendMessage[message_id] = Notify.create({
          group: false,
          type: 'ongoing',
          position: 'bottom-left',
          message: notificationString,
          html: true,
        })
      } else {
        // Update the current status message
        $unmanic.frontendMessage[message_id]({
          message: notificationString,
          html: true,
        })
      }
    }

    function displayNotice($t, message_id, type, code, message, timeout) {
      if (!(message_id in $unmanic.frontendMessage)) {
        // Fetch message string from i18n
        let notificationStringId = 'notifications.serverMessages.' + code
        let notificationString = $t(notificationStringId)
        // If i18n doesnt have this string ID, then revert to default
        if (notificationString === notificationStringId) {
          notificationString = $t('notifications.serverMessages.defaults.' + type);
        }
        // If the message is not empty, concatenate it to the end of the notification string
        if (message) {
          notificationString = notificationString + ' - ' + message;
        }

        // Format notification based on message type
        let color = 'info';
        let icon = 'announcement';
        if (type === 'error') {
          color = 'negative';
          icon = 'error';
        } else if (type === 'warning') {
          color = 'warning';
          icon = 'warning';
        } else if (type === 'success') {
          color = 'positive';
          icon = 'thumb_up';
        }

        $unmanic.frontendMessage[message_id] = Notify.create({
          timeout: timeout,
          color: color,
          position: 'bottom-right',
          message: notificationString,
          icon: icon,
          actions: [
            {
              icon: 'close',
              color: 'white',
              handler: () => {
                dismissMessages(message_id);
              }
            }
          ]
        })
      }
    }

    function displayMessages(data) {
      if (typeof $unmanic.frontendMessage === 'undefined') {
        $unmanic.frontendMessage = {};
      }
      let current_ids = []
      for (let i = 0; i < data.length; i++) {
        let message_id = data[i].id
        let type = data[i].type
        let code = data[i].code
        let message = data[i].message
        let timeout = data[i].timeout
        if (type === 'status') {
          displayStatus($t, message_id, type, code, message, timeout);
        } else {
          displayNotice($t, message_id, type, code, message, timeout);
        }
        current_ids[current_ids.length] = message_id
      }
      for (let message_id in $unmanic.frontendMessage) {
        if (!(current_ids.includes(message_id))) {
          dismissMessages(message_id);
        }
      }
    }

    if (!manager.baseListenersRegistered) {
      manager.baseListenersRegistered = true;
      // Add event listener to request frontend messages from server
      registerListener('open', 'start_frontend_messages', function (evt) {
        clearTimeout(manager.reconnectTimer);
        manager.reconnectTimer = null;
        dismissConnectionWarning();
        evt.currentTarget.send(JSON.stringify({ command: 'start_frontend_messages', params: {} }));
      }, null);

      // Add event listener to handle frontend messages from server
      registerListener('message', 'handle_frontend_messages', function (evt) {
        if (typeof evt.data === 'string') {
          let jsonData = JSON.parse(evt.data);
          if (jsonData.success) {
            // Ensure the server is still running the same instance...
            if (manager.serverId === null) {
              manager.serverId = jsonData.server_id;
            } else {
              if (jsonData.server_id !== manager.serverId) {
                // Reload the whole page. Some things may have changed
                console.debug('Unmanic server has restarted. Reloading page...');
                location.reload();
              }
            }
            // Parse data type and update the dashboard
            switch (jsonData.type) {
              case 'frontend_message':
                displayMessages(jsonData.data);
                break;
            }
          } else {
            console.error('WebSocket Error: Received contained errors - ', evt.data);
          }
        } else {
          console.error('WebSocket Error: Received data was not a string - ', evt.data);
        }
      }, null);

      // Add event listener to handle an error in the websocket
      registerListener('error', 'websocket_error', function (evt) {
        console.error('WebSocket Error: ', evt);
        // Set a timeout before displaying disconnect warning.
        // Sometimes we get a disconnect just from a slow connection.
        clearTimeout(manager.warningTimer);
        manager.warningTimer = setTimeout(() => {
          manager.warningTimer = null;
          // Display error
          showWebsocketConnectionWarning();
        }, 5000);
      }, null);

      // Add event listener to auto-reconnect the websocket if the socket closes
      registerListener('close', 'websocket_close', function (evt) {
        if ($unmanic.ws === evt.currentTarget) {
          $unmanic.ws = null;
        }
        if (!manager.intentionalClose && manager.owners.size > 0) {
          scheduleReconnect();
        }
      }, null);
    }

    manager.owners.add(owner);
    if (typeof $unmanic.ws === 'undefined' || $unmanic.ws === null) {
      console.debug("Starting connection to websocket server")
      openWS();
    }

    return $unmanic.ws;
  }

  /**
   * Add an event listener to the websocket.
   * This allows us to ensure that event listeners are not duplicated.
   *
   * @param type
   * @param key
   * @param callback
   */
  const addWebsocketEventListener = function (type, key, callback) {
    registerListener(type, key, callback);
  }

  /**
   * Close the websocket without triggering a reconnect
   */
  const closeWebsocket = function () {
    manager.owners.delete(owner);
    manager.listeners.forEach((definition, key) => {
      if (definition.owner !== owner) {
        return;
      }
      if ($unmanic.ws) {
        const wrapper = definition.wrappers.get($unmanic.ws);
        if (wrapper) {
          $unmanic.ws.removeEventListener(definition.type, wrapper);
        }
      }
      manager.listeners.delete(key);
    });

    if (manager.owners.size === 0) {
      manager.intentionalClose = true;
      clearTimeout(manager.reconnectTimer);
      clearTimeout(manager.manualReconnectTimer);
      manager.reconnectTimer = null;
      manager.manualReconnectTimer = null;
      dismissConnectionWarning();
      if ($unmanic.ws) {
        console.debug("Closing connection to websocket server")
        const socket = $unmanic.ws;
        $unmanic.ws = null;
        manager.generation += 1;
        socket.close();
      }
      manager.intentionalClose = false;
    }
  }

  const reconnectWebsocket = function () {
    manager.owners.add(owner);
    clearTimeout(manager.reconnectTimer);
    manager.intentionalClose = true;
    const previousSocket = $unmanic.ws;
    $unmanic.ws = null;
    manager.generation += 1;

    return new Promise((resolve, reject) => {
      let connectionStarted = false;
      const connect = () => {
        if (connectionStarted) {
          return;
        }
        clearTimeout(manager.manualReconnectTimer);
        manager.manualReconnectTimer = null;
        if (manager.owners.size === 0) {
          manager.intentionalClose = false;
          reject(new Error('WebSocket connection cancelled'));
          return;
        }
        connectionStarted = true;
        manager.intentionalClose = false;
        const socket = openWS();
        if (!socket) {
          reject(new Error('WebSocket connection cancelled'));
          return;
        }
        if (socket.readyState === WebSocket.OPEN) {
          resolve(socket);
          return;
        }
        socket.addEventListener('open', () => resolve(socket), { once: true });
        socket.addEventListener('error', () => reject(new Error('WebSocket connection failed')), { once: true });
      };

      if (previousSocket && previousSocket.readyState !== WebSocket.CLOSED) {
        previousSocket.addEventListener('close', connect, { once: true });
        previousSocket.close();
        manager.manualReconnectTimer = setTimeout(connect, 1000);
      } else {
        connect();
      }
    });
  }

  return {
    get serverId() {
      return manager.serverId;
    },
    get generation() {
      return manager.generation;
    },
    init: function () {
      return initWebsocket();
    },
    reconnect: function () {
      return reconnectWebsocket();
    },
    isCurrentSocket: function (socket) {
      return socket === $unmanic.ws && socket.__unmanicGeneration === manager.generation;
    },
    close: function () {
      closeWebsocket();
    },
    addEventListener: function (type, key, callback) {
      addWebsocketEventListener(type, key, callback);
    }
  }
}

export default {
  UnmanicWebsocketHandler,
}
