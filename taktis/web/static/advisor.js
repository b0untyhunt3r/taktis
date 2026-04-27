/**
 * Advisor Panel — shared JS for floating advisor chat widget
 * Used by: task_detail.html, project_detail.html, projects.html
 */
(function() {
  'use strict';

  var _advisorState = {
    contextType: null,
    contextId: null,
    contextDataFn: null,  // optional function returning extra context (projects.html)
    token: null,
    eventSource: null,
    popup: null,
    fab: null,
    messagesEl: null,
    inputEl: null,
    welcomeEl: null,
    sessionKey: null,
    renderTimer: null,
    pendingMarkdown: '',
    currentAssistantBody: null,
    initialized: false
  };

  function getCsrf() {
    var m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
    return m ? m[1] : '';
  }

  function timeStr() {
    var d = new Date();
    var h = d.getHours();
    var m = d.getMinutes();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
  }

  function shouldAutoScroll(container) {
    return container.scrollHeight - container.scrollTop - container.clientHeight < 80;
  }

  function hideWelcome() {
    if (_advisorState.welcomeEl) {
      _advisorState.welcomeEl.style.display = 'none';
    }
  }

  function createMsgEl(role) {
    var msg = document.createElement('div');
    msg.className = 'advisor-msg advisor-msg-' + role;

    var avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    if (role === 'assistant') {
      avatar.innerHTML = '<img src="/static/favicon.svg" alt="" width="16" height="16" style="border-radius:2px;">';
    } else {
      avatar.textContent = 'You';
    }

    var content = document.createElement('div');
    content.className = 'msg-content';

    var body = document.createElement('div');
    body.className = 'msg-body';
    content.appendChild(body);

    var time = document.createElement('div');
    time.className = 'msg-time';
    time.textContent = timeStr();
    content.appendChild(time);

    msg.appendChild(avatar);
    msg.appendChild(content);

    return { msg: msg, body: body };
  }

  function appendUserMsg(text) {
    hideWelcome();
    var els = createMsgEl('user');
    els.body.textContent = text;
    var container = _advisorState.messagesEl;
    container.appendChild(els.msg);
    container.scrollTop = container.scrollHeight;
  }

  function showTypingIndicator() {
    removeTypingIndicator();
    var typing = document.createElement('div');
    typing.className = 'advisor-typing';
    typing.id = 'advisor-typing-indicator';

    var avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.innerHTML = '<img src="/static/favicon.svg" alt="" width="16" height="16" style="border-radius:2px;">';

    var dots = document.createElement('div');
    dots.className = 'advisor-typing-dots';
    dots.innerHTML = '<span></span><span></span><span></span>';

    typing.appendChild(avatar);
    typing.appendChild(dots);

    var container = _advisorState.messagesEl;
    container.appendChild(typing);
    container.scrollTop = container.scrollHeight;
  }

  function removeTypingIndicator() {
    var el = document.getElementById('advisor-typing-indicator');
    if (el) el.remove();
  }

  function renderMarkdownThrottled() {
    if (_advisorState.renderTimer) return;
    _advisorState.renderTimer = setTimeout(function() {
      _advisorState.renderTimer = null;
      if (_advisorState.currentAssistantBody && _advisorState.pendingMarkdown) {
        var container = _advisorState.messagesEl;
        var doScroll = shouldAutoScroll(container);
        _advisorState.currentAssistantBody.innerHTML =
          DOMPurify.sanitize(marked.parse(_advisorState.pendingMarkdown));
        _advisorState.currentAssistantBody.classList.add('md-output');
        if (doScroll) container.scrollTop = container.scrollHeight;
      }
    }, 250);
  }

  function finalizeAssistantMsg() {
    if (_advisorState.currentAssistantBody && _advisorState.pendingMarkdown) {
      _advisorState.currentAssistantBody.innerHTML =
        DOMPurify.sanitize(marked.parse(_advisorState.pendingMarkdown));
      _advisorState.currentAssistantBody.classList.add('md-output');
      // Apply code highlighting only on done
      _advisorState.currentAssistantBody.querySelectorAll('pre code').forEach(function(block) {
        if (typeof hljs !== 'undefined') hljs.highlightElement(block);
      });
      var container = _advisorState.messagesEl;
      container.scrollTop = container.scrollHeight;
    }
    _advisorState.pendingMarkdown = '';
    _advisorState.currentAssistantBody = null;
    if (_advisorState.renderTimer) {
      clearTimeout(_advisorState.renderTimer);
      _advisorState.renderTimer = null;
    }
  }

  function startSSE(token) {
    if (_advisorState.eventSource) {
      _advisorState.eventSource.close();
      _advisorState.eventSource = null;
    }

    _advisorState.pendingMarkdown = '';
    _advisorState.currentAssistantBody = null;

    _advisorState.eventSource = new EventSource('/events/consult/' + token);

    _advisorState.eventSource.onmessage = function(e) {
      var data = JSON.parse(e.data);
      if (data.type === 'text') {
        if (!_advisorState.currentAssistantBody) {
          removeTypingIndicator();
          hideWelcome();
          var els = createMsgEl('assistant');
          _advisorState.messagesEl.appendChild(els.msg);
          _advisorState.currentAssistantBody = els.body;
        }
        _advisorState.pendingMarkdown += (data.text || '');
        renderMarkdownThrottled();
      }
    };

    _advisorState.eventSource.addEventListener('done', function() {
      _advisorState.eventSource.close();
      _advisorState.eventSource = null;
      finalizeAssistantMsg();
    });

    _advisorState.eventSource.onerror = function() {
      _advisorState.eventSource.close();
      _advisorState.eventSource = null;
      finalizeAssistantMsg();
    };
  }

  // Auto-resize textarea
  window.autoResizeTextarea = function(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  };

  window.toggleAdvisor = function() {
    var popup = _advisorState.popup;
    var fab = _advisorState.fab;
    if (!popup) return;
    var isOpen = popup.classList.toggle('open');
    if (fab) fab.classList.toggle('open', isOpen);
    if (isOpen && !_advisorState.token) {
      var payload = {};
      if (_advisorState.contextDataFn) {
        payload.context_type = _advisorState.contextType;
        payload.context_data = _advisorState.contextDataFn();
      } else {
        payload.context_type = _advisorState.contextType;
        payload.context_id = _advisorState.contextId;
      }
      fetch('/api/consult', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrf()},
        body: JSON.stringify(payload)
      })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.token) {
          _advisorState.token = data.token;
          sessionStorage.setItem(_advisorState.sessionKey, data.token);
        }
      });
    }
    if (isOpen && _advisorState.inputEl) {
      setTimeout(function() { _advisorState.inputEl.focus(); }, 100);
    }
  };

  // Keep old name as alias
  window.toggleConsult = window.toggleAdvisor;

  window.sendConsult = function() {
    var input = _advisorState.inputEl;
    if (!input) return;
    var text = input.value.trim();
    if (!text || !_advisorState.token) return;
    input.value = '';
    input.style.height = 'auto';
    appendUserMsg(text);
    showTypingIndicator();

    var body = { message: text };
    if (_advisorState.contextDataFn) {
      body.context_data = _advisorState.contextDataFn();
    }

    fetch('/api/consult/' + _advisorState.token + '/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrf()},
      body: JSON.stringify(body)
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.ok) {
        startSSE(_advisorState.token);
      } else {
        removeTypingIndicator();
        var els = createMsgEl('assistant');
        els.body.textContent = 'Error: ' + (data.error || 'unknown');
        _advisorState.messagesEl.appendChild(els.msg);
      }
    })
    .catch(function(err) {
      removeTypingIndicator();
      var els = createMsgEl('assistant');
      els.body.textContent = 'Error: ' + err;
      _advisorState.messagesEl.appendChild(els.msg);
    });
  };

  /**
   * Initialize the advisor panel.
   * @param {string} contextType - 'task', 'project', or 'project_create'
   * @param {string} contextId - the task/project ID
   * @param {object} opts - optional: { popupId, contextDataFn }
   */
  window.initAdvisor = function(contextType, contextId, opts) {
    opts = opts || {};
    var popupId = opts.popupId || 'advisor-popup';
    _advisorState.contextType = contextType;
    _advisorState.contextId = contextId;
    _advisorState.contextDataFn = opts.contextDataFn || null;
    _advisorState.sessionKey = 'consult-' + contextType + '-' + (contextId || 'new');
    _advisorState.token = sessionStorage.getItem(_advisorState.sessionKey);
    _advisorState.popup = document.getElementById(popupId);
    _advisorState.fab = document.getElementById('advisor-fab');
    _advisorState.messagesEl = _advisorState.popup ? _advisorState.popup.querySelector('.advisor-messages') : null;
    _advisorState.inputEl = _advisorState.popup ? _advisorState.popup.querySelector('.advisor-input-wrap textarea') : null;
    _advisorState.welcomeEl = _advisorState.popup ? _advisorState.popup.querySelector('.advisor-welcome') : null;
    _advisorState.initialized = true;

    // Cleanup on page unload
    window.addEventListener('beforeunload', function() {
      if (_advisorState.eventSource) {
        _advisorState.eventSource.close();
      }
      if (_advisorState.token) {
        fetch('/api/consult/' + _advisorState.token, {
          method: 'DELETE',
          keepalive: true,
          headers: {'X-CSRFToken': getCsrf()}
        });
        sessionStorage.removeItem(_advisorState.sessionKey);
      }
    });
  };
})();
