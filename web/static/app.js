"use strict";
(function () {
  const {
    RealtimeChart, DataGrid, sparkline, animateNumber, conditionBar, evidencePanel, GFMT, fmtTermVal,
    CandleChart, tradeChart,
    CONFIG_SCHEMA, fieldTier, buildField, collectConfig, getPath, setPath, techBadge, techBadgeHTML, loadTechniques,
    icon,
  } = window.UI;

  // ━━ 유틸 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function $$(sel, root) {
    return Array.from((root || document).querySelectorAll(sel));
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach((k) => {
      const v = attrs[k];
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.slice(0, 2) === "on" && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
      else if (k in node) node[k] = v;
      else node.setAttribute(k, v);
    });
    (children || []).forEach((c) => {
      if (c == null) return;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return node;
  }

  // ★ 종목명·테마명처럼 외부(증권사 API·테마 파일)에서 온 값을 HTML 문자열에 넣을 때 쓴다.
  //   서버가 CSP 로 인라인 스크립트를 막지만, 화면이 깨지거나 남의 마크업이 끼어드는 것도 막는다.
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function won(v) {
    if (v == null || isNaN(v)) return "-";
    return Math.round(v).toLocaleString("ko-KR") + "원";
  }

  function signed(v, unit) {
    if (v == null || isNaN(v)) return "-";
    const sign = v > 0 ? "+" : "";
    if (unit === "won") return sign + Math.round(v).toLocaleString("ko-KR") + "원";
    return sign + Math.round(v).toLocaleString("ko-KR") + (unit || "");
  }

  function pct(v, digits) {
    if (v == null || isNaN(v)) return "-";
    digits = digits == null ? 2 : digits;
    const sign = v > 0 ? "+" : "";
    return sign + (v * 100).toFixed(digits) + "%";
  }

  // ★ 승률처럼 "수준"을 나타내는 값에는 부호를 붙이지 않는다 - 붙이면
  // 변화량("+53%")처럼 잘못 읽힌다.
  function pct0(v) {
    if (v == null || isNaN(v)) return "-";
    return Math.round(v * 100) + "%";
  }

  function eok(v) {
    if (v == null || isNaN(v)) return "-";
    return (v / 1e8).toLocaleString("ko-KR", { maximumFractionDigits: 1 }) + "억";
  }

  function dir(v) {
    if (v > 0) return "rise";
    if (v < 0) return "fall";
    return "";
  }

  // ★★★ 비밀번호 재확인 토큰(scope → 토큰). 자동매매 조작·설정 변경 같은 민감한 요청은 서버가
  // "방금 비밀번호를 다시 입력했다"는 이 토큰이 있어야 받아 준다(화면의 재확인창은 이 토큰을 얻는 통로).
  // 토큰은 짧게 살고, 진짜 보안 경계는 서버다 - 여기 있는 토큰이 만료돼서 서버가 거절하면 api() 가
  // 알아서 다시 물어본다(아래 askConfirm 참고).
  // ★★★ "설정 메뉴에서 새로고침하면 비번을 또 물어본다" - 메모리에만 두면 새로고침 한 번에
  // 다 날아가, 방금 입력하고도 또 입력해야 했다. 탭을 닫으면 함께 지워지는 sessionStorage 에
  // 같이 적어 두고 부팅 시 다시 읽어 들인다(localStorage 는 브라우저를 꺼도 안 지워져서 안
  // 된다 - "새로 방문한 사람은 다시 입력해야 한다"는 원래 의도가 깨진다). 여기서 불러온
  // 토큰이 실제로는 만료됐어도 안전하다 - 위 설명대로 서버가 진짜 경계라 api() 가 알아서
  // 다시 물어본다.
  const _confirmTokens = {};
  const CONFIRM_TOKENS_KEY = "ui.confirmTokens";
  function _saveConfirmTokens() {
    try { sessionStorage.setItem(CONFIRM_TOKENS_KEY, JSON.stringify(_confirmTokens)); } catch (e) { /* 사생활 보호 모드 등 - 무시 */ }
  }
  function _loadConfirmTokens() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(CONFIRM_TOKENS_KEY) || "{}");
      if (saved && typeof saved === "object") Object.assign(_confirmTokens, saved);
    } catch (e) { /* 무시 */ }
  }
  function _clearConfirmTokens() {
    Object.keys(_confirmTokens).forEach((k) => { delete _confirmTokens[k]; });
    try { sessionStorage.removeItem(CONFIRM_TOKENS_KEY); } catch (e) { /* 무시 */ }
  }
  let _confirmPending = null;

  function askConfirm(scope) {
    if (_confirmPending) return _confirmPending;
    const settings = scope === "settings";
    _confirmPending = new Promise((resolve, reject) => {
      confirmPassword(resolve, {
        scope,
        title: settings ? "🔒 설정" : "🔒 자동매매 조작",
        hint: settings ? "설정을 바꾸려면 비밀번호를 다시 입력하세요." : "계속하려면 비밀번호를 다시 입력하세요.",
        onCancel: () => reject(new Error("비밀번호 확인을 취소했습니다.")),
      });
    }).finally(() => { _confirmPending = null; });
    return _confirmPending;
  }

  // ━━ 자리를 비우면 잠금 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // 마우스·키보드·터치가 idle_minutes(서버 설정) 동안 없으면 화면을 잠그고 비밀번호를 다시 받는다.
  // 서버도 같은 기준으로 세션을 끝내므로(조작 없이 자동 갱신만 있으면 연장되지 않는다) 화면을 못 보게 막아도 API 로는 못 들어온다.
  let _lastActive = Date.now();
  let _idleMinutes = 0;
  let _locked = false;
  ["pointerdown", "keydown", "wheel", "touchstart", "mousemove"].forEach((t) => {
    window.addEventListener(t, () => { _lastActive = Date.now(); }, { passive: true, capture: true });
  });

  function _lockScreen() {
    if (_locked) return;
    _locked = true;
    try { if (CONN && CONN.es) CONN.es.close(); } catch (e) { /* 이미 닫힘 */ }
    showLogin();
  }

  setInterval(() => {
    if (_locked || !_idleMinutes) return;
    if (Date.now() - _lastActive > _idleMinutes * 60000) _lockScreen();
  }, 15000);

  async function api(path, opts) {
    opts = opts || {};
    const fetchOpts = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) {
      fetchOpts.headers["Content-Type"] = "application/json";
      fetchOpts.body = JSON.stringify(opts.body);
    }
    const held = Object.keys(_confirmTokens).map((k) => _confirmTokens[k]).filter(Boolean).join(",");
    if (held) fetchOpts.headers["X-Confirm-Token"] = held;
    // 사용자가 최근에 화면을 조작했을 때만 서버에 알려 세션을 연장한다(자동 갱신은 조작이 아니다).
    if (Date.now() - _lastActive < 60000) fetchOpts.headers["X-User-Active"] = "1";
    const res = await fetch(path, fetchOpts);
    if (res.status === 401 && path !== "/api/login" && path !== "/api/auth/check") {
      _lockScreen();  // 세션이 끝났다 - 화면을 잠그고 비밀번호를 다시 받는다.
    }
    let body = null;
    try {
      body = await res.json();
    } catch (e) {
      body = null; // 본문이 없는 응답(204 등)일 수 있다.
    }
    if (res.status === 403 && body && body.confirm_required && !opts._retried) {
      // 재확인 토큰이 없거나 만료됐다 - 비밀번호를 다시 받은 뒤 같은 요청을 한 번만 재시도한다.
      await askConfirm(body.confirm_required);
      return api(path, Object.assign({}, opts, { _retried: true }));
    }
    if (!res.ok) {
      const msg = (body && body.error) || res.statusText || "요청에 실패했습니다.";
      throw new Error(msg);
    }
    return body;
  }

  let _toastTimer = null;
  function toast(msg, kind) {
    let box = $(".toast");
    if (!box) {
      box = el("div", { class: "toast" });
      document.body.appendChild(box);
    }
    box.textContent = msg;
    box.dataset.kind = kind || "info";
    box.style.display = "block";
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => {
      box.style.display = "none";
    }, 3000);
  }

  function busy(container, isBusy) {
    if (!container) return;
    container.classList.toggle("is-busy", !!isBusy);
    container.setAttribute("aria-busy", isBusy ? "true" : "false");
  }

  // ★ 내용이 그대로면 DOM 을 건드리지 않는다 - 폴링/스냅샷마다 지우고 다시 그리면 화면이 깜빡이고
  //   스크롤·접힘 상태도 초기화된다. 캔버스(차트)가 든 노드는 호출부가 같은 노드를 재사용한다.
  function fill(container, node) {
    if (!container) return;
    if (node == null) {
      if (container.firstChild) container.innerHTML = "";
      container._sig = null;
      return;
    }
    if (container.firstChild === node && container.childNodes.length === 1) return;
    if (!node.querySelector || !node.querySelector("canvas")) {
      const sig = node.outerHTML;
      if (sig != null && container._sig === sig && container.firstChild) return;
      container._sig = sig;
    } else {
      container._sig = null;
    }
    container.innerHTML = "";
    container.appendChild(node);
  }

  // 여러 노드를 한 컨테이너에 나란히 채울 때(보유/감시 카드) - 전체 HTML 이 같으면 그대로 둔다.
  function fillAll(container, nodes) {
    if (!container) return;
    const sig = nodes.map((n) => n.outerHTML).join("");
    if (container._sig === sig && container.firstChild) return;
    container._sig = sig;
    container.innerHTML = "";
    nodes.forEach((n) => container.appendChild(n));
  }

  function skeleton(height) {
    return el("div", { class: "skel", style: { height: (height || 80) + "px" } });
  }

  // ━━ 자동 갱신 스크롤 보존 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★★★ "자동 갱신되면 화면이 맨 위로 튄다" - 종목선정/시세목록/실험실처럼
  // 긴 목록이 있는 패널은 주기적으로 통째로(또는 큰 덩어리로) 지우고 다시
  // 그린다. 그 사이 문서 높이가 잠깐 줄어들면(특히 innerHTML="" 로 비운 뒤
  // await 로 한 틱 넘어가는 경우) 브라우저가 스크롤 위치를 맞는 범위로
  // 당겨버려 맨 위로 튄 것처럼 보인다. 이 두 헬퍼는 실제로 스크롤하는
  // 요소(이 화면들은 전부 문서 전체 - <main>/.panel 은 자체 overflow 가
  // 없다. .console/.config-scroll 처럼 자체 overflow-y 를 가진 상자는
  // 따로 그 상자 기준으로 처리한다)의 위치를 다시 그리기 직전에 저장해
  // 뒀다가, 레이아웃이 끝난 다음 프레임에 그대로 되돌린다. 모든 렌더에
  // 무조건 거는 전역 훅이 아니라, 실제로 스크롤이 튀는 걸 확인한 자리에서만
  // 명시적으로 불러 쓴다(자리마다 실제로 스크롤하는 요소가 다를 수 있어서다).
  function _scrollAnchorEl() {
    return document.scrollingElement || document.documentElement;
  }
  function _restoreScrollAfter(savedTop, scrollEl) {
    scrollEl = scrollEl || _scrollAnchorEl();
    requestAnimationFrame(() => {
      if (scrollEl.scrollTop !== savedTop) scrollEl.scrollTop = savedTop;
    });
  }

  // ━━ 내비게이션 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★ NAV 는 [{group, hint, tabs:[[id, label], …]}] 배열 하나로 둔다.
  // E코스가 여기에 news·market·review·lab 을 끼워 넣는다. 하드코딩하면
  // 그때 화면 전체를 고쳐야 한다.

  // ★★★ "아이콘도 세련되고 깔끔한 걸로, 심플한 걸로" - 플랫폼마다 다르게 그려지고
  // 색이 고정된 이모지 대신, ui.js 의 UI.icon() 이 만드는 currentColor 얇은 선
  // 아이콘(SVG)을 쓴다. 그룹(4개) 은 하단 탭바·사이드바 섹션 아이콘으로,
  // 각 화면(16개) 은 사이드바 항목 아이콘으로 쓴다.
  const GROUP_ICON = { Trading: "trading", Records: "records", Improve: "improve", Settings: "settings" };
  const GROUP_LABEL_KO = { Trading: "매매", Records: "기록", Improve: "개선", Settings: "설정" };
  const TAB_ICON = {
    dash: "dashboard", selection: "target", news: "newspaper", market: "globe",
    perf: "chart", journal: "book", marketreview: "message",
    about: "info", review: "calendar", lab: "flask", playbook: "layers", rules: "shield", release: "tag",
    setup: "plug", themes: "hash", config: "sliders",
  };

  const NAV = [
    { group: "Trading", hint: "지금 무슨 일이 일어나고 있는지",
      tabs: [["dash", "대시보드"], ["selection", "종목 선정"], ["news", "속보"], ["market", "시장"]] },
    { group: "Records", hint: "무엇을 샀고 왜 그랬는지",
      tabs: [["perf", "성과"], ["journal", "매매일지"], ["marketreview", "시장 평가"]] },
    { group: "Improve", hint: "규칙을 이해하고 다듬기",
      tabs: [["about", "소개"], ["review", "월간 리뷰"], ["lab", "실험실"], ["playbook", "매매 기법"], ["rules", "매매원칙"], ["release", "릴리즈 노트"]] },
    { group: "Settings", hint: "연결과 파라미터",
      tabs: [["setup", "준비·연결"], ["themes", "테마"], ["config", "설정"]] },
  ];

  // ★★★ 한 화면에 섹션이 길게 쌓여 있던 메뉴(매매 기법·매매원칙·준비·연결)를 탭으로 나눈다.
  // tabs: [{ id, label, build() -> Node }]. 선택한 탭은 key 로 기억해서, 화면이 다시 그려져도
  // (다른 메뉴에 갔다 오거나 자동 갱신) 보던 탭이 유지된다.
  const _tabState = {};
  function renderTabs(panel, key, tabs) {
    if (!tabs.length) return;
    if (!tabs.some((t) => t.id === _tabState[key])) _tabState[key] = tabs[0].id;
    const seg = el("div", { class: "seg", style: { margin: "var(--s2) 0" } });
    const body = el("div");
    const paint = () => {
      body.innerHTML = "";
      const t = tabs.find((x) => x.id === _tabState[key]);
      const node = t && t.build();
      if (node) body.appendChild(node);
    };
    tabs.forEach((t) => {
      seg.appendChild(el("button", {
        text: t.label, class: t.id === _tabState[key] ? "active" : "",
        onclick: (e) => {
          _tabState[key] = t.id;
          $$("button", seg).forEach((b) => b.classList.remove("active"));
          e.target.classList.add("active");
          paint();
        },
      }));
    });
    panel.appendChild(seg);
    panel.appendChild(body);
    paint();
  }

  const _panels = {}; // id -> { onShow(), ... }
  let _activeTab = null;

  function registerPanel(id, handlers) {
    _panels[id] = handlers || {};
  }

  function allTabIds() {
    return NAV.reduce((acc, g) => acc.concat(g.tabs.map((t) => t[0])), []);
  }

  function showTab(id, opts) {
    opts = opts || {};
    const ids = allTabIds();
    if (ids.indexOf(id) < 0) id = ids[0];

    // ★★★ "설정 메뉴를 열 때마다 비밀번호를 다시 확인해달라" - 로그인
    // 세션이 이미 있어도(자리를 비운 사이 잠금 안 된 화면을 남이 주웠을
    // 때) 설정만큼은 한 번 더 막는다. 같은 Settings 그룹 안의 하위 탭
    // (준비·연결/테마/설정)끼리 옮겨 다닐 때는 다시 안 묻는다 - 그룹
    // 바깥에서 "새로" 들어올 때만 확인한다.
    // ★ 통과 후 재호출은 반드시 _reallyShowTab() 으로 - showTab() 을 다시
    // 부르면 그 시점엔 아직 _activeGroup 이 "Settings" 로 안 바뀐 채라
    // 이 조건에 또 걸려서, 성공해도 모달이 닫혔다 곧바로 다시 열리는
    // 무한루프가 됐었다(실제로 겪음).
    // ★ 새로고침 직후 설정 하위 탭 해시(#config 등)로 바로 들어올 때도, sessionStorage 에서
    // 막 불러온 재확인 토큰이 이미 있으면(위 _loadConfirmTokens 참고) 다시 안 묻는다 - 토큰이
    // 실제로 만료됐어도 안전하다(서버가 거절하면 api() 가 그때 다시 물어본다).
    const owningGroupForGuard = NAV.find((g) => g.tabs.some(([tid]) => tid === id));
    if (owningGroupForGuard && owningGroupForGuard.group === "Settings" && _activeGroup !== "Settings" && !_confirmTokens.settings) {
      confirmPassword(() => _reallyShowTab(id, opts), {
        scope: "settings", title: "🔒 설정", hint: "설정 메뉴에 들어가려면 비밀번호를 다시 입력하세요.",
      });
      return;
    }

    _reallyShowTab(id, opts);
  }

  function _reallyShowTab(id, opts) {
    _activeTab = id;

    // ★ 탭이 속한 그룹을 찾아 그룹탭도 함께 활성화한다 - 즐겨찾기·URL
    // 해시로 바로 하위 탭에 진입해도 상위 그룹이 어긋나지 않는다.
    const owningGroup = NAV.find((g) => g.tabs.some(([tid]) => tid === id));
    if (owningGroup) {
      // ★★★ "그룹을 다시 탭하면 그 그룹에서 마지막으로 보던 화면으로" -
      // 하단 탭바(모바일)가 이 값을 읽어 첫 탭이 아니라 마지막 위치로
      // 돌아간다. 새로고침 사이엔 안 남아도 된다(세션 안에서만 기억).
      _lastTabInGroup[owningGroup.group] = id;
      if (owningGroup.group !== _activeGroup) {
        _activeGroup = owningGroup.group;
        renderGroupNav();
        renderSubNav();
      }
    }

    $$(".sub-nav a[data-tab]").forEach((a) => a.classList.toggle("active", a.dataset.tab === id));
    _updateSidebarActive();
    $$("main .panel").forEach((p) => {
      p.style.display = p.dataset.panel === id ? "flex" : "none";
    });

    // ★ 상단바 제목 - 사이드바를 접어도(레일 모드) 지금 어느 화면인지는
    // 항상 여기서 보인다.
    const title = $("#topbar-title");
    if (title && owningGroup) {
      const tabDef = owningGroup.tabs.find(([tid]) => tid === id);
      title.textContent = tabDef ? tabDef[1] : id;
    }

    // ★ 탭이 바뀌면 해외주식 통화 토글(#currency-toggle)도 새 탭 기준으로 다시 보이거나
    // 숨는다 - _currencyToggleRelevant() 참고. initCurrencyToggle() 이 아직 버튼을 만들기
    // 전(로그인 전 초기 showTab 등)이면 $("#currency-toggle") 이 없어도 조용히 넘어간다.
    if (typeof _updateCurrencyToggleVisibility === "function") _updateCurrencyToggleVisibility();

    if (!opts.silent) {
      history.replaceState(null, "", "#" + id);
    }

    // ★★★ "메뉴 클릭할 때마다 매번 동기화할 필요는 없다 - 이미 동기화된 건 다시 안 해도
    // 된다" - 패널을 열 때마다 API 를 다시 불러서 느려지고 입력 중이던 화면도 날아갔다.
    // cacheMs 가 있는 패널은 그 시간 안에 다시 열면 이미 그려 둔 화면을 그대로 보여준다.
    // (실시간 패널·데이터가 바뀌는 조작 뒤에는 cacheMs 를 안 주거나 직접 다시 그린다.)
    const handlers = _panels[id];
    if (handlers && typeof handlers.onShow === "function") {
      const fresh = handlers.cacheMs && handlers._loadedAt
        && (Date.now() - handlers._loadedAt) < handlers.cacheMs && !opts.force;
      if (!fresh) {
        handlers._loadedAt = Date.now();
        const r = handlers.onShow();
        if (r && typeof r.catch === "function") r.catch(() => { handlers._loadedAt = 0; });
      }
    }
  }

  let _activeGroup = null;
  const _lastTabInGroup = {}; // group -> 그 그룹에서 마지막으로 본 tab id (세션 중에만 기억)

  function buildNav() {
    renderSidebarNav();
    renderGroupNav();
    initSidebarCollapse();
  }

  // ━━ 사이드바(데스크톱·태블릿) - 16개 화면을 그룹별 섹션으로 전부 펼쳐
  // 둔다. "많은 스크롤로 보기 불편한 네비게이션 지양" 요청대로, 어느
  // 화면이든 여기서 한 번만 누르면 간다. ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  function renderSidebarNav() {
    const nav = $("#sidebar-nav");
    if (!nav) return;
    nav.innerHTML = "";

    const logo = $(".sidebar-brand-logo");
    if (logo && !logo.innerHTML) logo.innerHTML = icon("trending-up", 16);

    NAV.forEach((g) => {
      const section = el("div", { class: "sidebar-group" });
      section.appendChild(el("div", { class: "sidebar-group-label", text: GROUP_LABEL_KO[g.group] || g.group }));
      g.tabs.forEach(([id, label]) => {
        const btn = el("button", {
          type: "button", class: "sidebar-item", "data-tab": id,
          title: label, "aria-label": label, "aria-current": "false",
        });
        btn.appendChild(el("span", { class: "sidebar-item-icon", html: icon(TAB_ICON[id] || "hash", 18) }));
        btn.appendChild(el("span", { class: "sidebar-item-label", text: label }));
        btn.addEventListener("click", () => showTab(id));
        section.appendChild(btn);
      });
      nav.appendChild(section);
    });
    _updateSidebarActive();
  }

  function _updateSidebarActive() {
    $$(".sidebar-item[data-tab]").forEach((b) => {
      const active = b.dataset.tab === _activeTab;
      b.classList.toggle("active", active);
      b.setAttribute("aria-current", active ? "page" : "false");
    });
  }

  // 데스크톱(≥1024px) 은 기본으로 펼치고, 태블릿(768~1023px) 은 기본으로
  // 아이콘 레일이다 - 사용자가 접기 버튼을 누르면 그 뒤로는 폭과 무관하게
  // localStorage 값을 따른다.
  const SIDEBAR_COLLAPSE_KEY = "ui.sidebarCollapsed";

  function _loadSidebarCollapsed() {
    try {
      const saved = localStorage.getItem(SIDEBAR_COLLAPSE_KEY);
      if (saved === "1") return true;
      if (saved === "0") return false;
    } catch (e) { /* 사생활 보호 모드 등 - 무시 */ }
    return !window.matchMedia("(min-width: 1024px)").matches;
  }

  function _applySidebarCollapsed(collapsed) {
    const shell = $(".app-shell");
    if (shell) shell.classList.toggle("sidebar-collapsed", collapsed);
    const btn = $("#sidebar-collapse");
    if (btn) btn.setAttribute("aria-expanded", String(!collapsed));
  }

  function initSidebarCollapse() {
    _applySidebarCollapsed(_loadSidebarCollapsed());
    const btn = $("#sidebar-collapse");
    if (!btn) return;
    if (!btn.innerHTML) btn.innerHTML = icon("chevron-left", 16);
    if (btn.dataset.bound) return; // buildNav() 는 로그인마다 한 번씩만 불리지만, 혹시 몰라 중복 바인딩을 막는다.
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      const shell = $(".app-shell");
      const next = !(shell && shell.classList.contains("sidebar-collapsed"));
      _applySidebarCollapsed(next);
      try { localStorage.setItem(SIDEBAR_COLLAPSE_KEY, next ? "1" : "0"); } catch (e) { /* 무시 */ }
    });
  }

  // ━━ 하단 탭바(모바일) - 그룹 4개만. 탭하면 그 그룹에서 마지막으로 보던
  // 화면으로 돌아간다(처음이면 그룹의 첫 화면). ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  function renderGroupNav() {
    const groupNav = $("#group-nav");
    if (!groupNav) return;
    groupNav.innerHTML = "";

    NAV.forEach((g) => {
      const btn = el("button", {
        type: "button", class: "group-tab", title: g.hint, "aria-label": g.hint,
      });
      btn.appendChild(el("span", { class: "group-tab-icon", html: icon(GROUP_ICON[g.group] || "hash", 20) }));
      btn.appendChild(el("span", { class: "group-tab-label", text: GROUP_LABEL_KO[g.group] || g.group }));
      btn.addEventListener("click", () => {
        // ★ 그룹 활성화는 showTab() 안에서 한 곳에서만 처리한다 - 여기서
        // 미리 _activeGroup 을 바꿔버리면 showTab() 이 "이미 그 그룹에
        // 있다"고 오판해서, 아래 설정 잠금 재확인이 걸리지 않는다.
        showTab(_lastTabInGroup[g.group] || g.tabs[0][0]);
      });
      groupNav.appendChild(btn);
    });

    const activeIdx = NAV.findIndex((g) => g.group === _activeGroup);
    Array.from(groupNav.children).forEach((btn, i) => {
      btn.classList.toggle("active", i === activeIdx);
      btn.setAttribute("aria-current", i === activeIdx ? "page" : "false");
    });
  }

  // ━━ 하위 탭 칩(모바일) - 지금 그룹 안의 다른 화면들을 가로로 미는 알약
  // 한 줄로. _activeTabIs() 등 다른 코드가 ".sub-nav a[data-tab]" 를 그대로
  // 찾으므로(아래 ~2280줄 부근), 데스크톱에서 숨겨도(CSS) 이 요소 자체는
  // 계속 만든다. ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  function renderSubNav() {
    const subNav = $("#sub-nav");
    if (!subNav) return;
    subNav.innerHTML = "";
    const group = NAV.find((g) => g.group === _activeGroup);
    if (!group) return;

    group.tabs.forEach(([id, label]) => {
      const a = el("a", { href: "#" + id, "data-tab": id, text: label });
      a.addEventListener("click", (e) => {
        e.preventDefault();
        showTab(id);
      });
      subNav.appendChild(a);
    });
    // 탭이 하나뿐인 그룹은 칩이 있어 봐야 고를 게 없다 - CSS 가 숨긴다.
    subNav.classList.toggle("single-tab", group.tabs.length <= 1);
  }

  // 숫자키 1~N 으로 그룹 이동 (입력칸에 포커스가 있으면 무시한다).
  document.addEventListener("keydown", (e) => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    const n = parseInt(e.key, 10);
    if (!isNaN(n) && n >= 1 && n <= NAV.length) {
      const g = NAV[n - 1];
      showTab(_lastTabInGroup[g.group] || g.tabs[0][0]);
    }
  });

  // ━━ 실시간 연결(SSE) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  const STREAM_EVENTS = ["snapshot", "tick", "log", "trade", "verdict", "alert", "selection"];
  const CONN = { mode: "connecting", es: null, failCount: 0, pollTimer: null, lastEventAt: 0 };

  // ★ "연결 중"이라고만 하면 무엇과 연결되는지 알 수 없다 - 이 화면이
  // 실제로 연결되는 대상은 인터넷 바깥이 아니라 "이 컴퓨터에서 실행 중인
  // AutoDayTrading 프로그램 자체(로컬 서버)"다. 라벨과 설명 둘 다에
  // 이걸 분명히 적는다.
  const CONN_EXPLAIN = {
    connecting: "이 화면이 실행 중인 AutoDayTrading 프로그램(내 컴퓨터, 인터넷 바깥 아님)과 연결을 시도하는 중입니다. 보통 1초 안에 '연결됨'으로 바뀝니다.",
    live: "AutoDayTrading 프로그램과 실시간으로 연결되어 있습니다. 매매·시세 변화가 화면에 바로 반영됩니다.",
    reconnecting: "AutoDayTrading 프로그램과의 연결이 순간 끊겨 다시 붙는 중입니다. 몇 초 안에 자동으로 복구됩니다.",
    polling: "실시간 연결이 3번 연속 실패해서 3초마다 직접 물어보는 방식으로 바뀌었습니다. "
      + "동작은 하지만 갱신이 약간 느립니다.",
    down: "AutoDayTrading 프로그램과 연결하지 못하고 있습니다. 프로그램이 실행 중인지 확인하세요.",
  };

  function setConnIndicator(mode) {
    CONN.mode = mode;
    const dot = $("#conn-indicator");
    if (!dot) return;
    const labels = {
      live: "프로그램 연결됨", polling: "폴링 중", reconnecting: "재연결 중",
      down: "프로그램과 끊김", connecting: "프로그램 연결 중",
    };
    // ★ 글자 라벨 대신 아이콘 하나로 - 모양까지 다르게 해서(색만으로 구분하지 않음) 알아보기 쉽게 한다.
    // (이모지 대신 UI.icon() 의 선 아이콘 - 재디자인 지침대로 아이콘 세트를 통일한다.)
    const iconNames = { live: "wifi", polling: "refresh", reconnecting: "refresh", down: "wifi-off", connecting: "clock" };
    dot.innerHTML = icon(iconNames[mode] || "alert", 16);
    dot.dataset.mode = mode;
    dot.setAttribute("aria-label", labels[mode] || mode);
    // ★ 마우스를 올리면(모바일은 눌러서) 왜 이 상태인지 바로 설명이 보이게 한다 -
    // "왜 계속 연결중이야?" 라는 질문에 화면 스스로 답할 수 있어야 한다.
    dot.title = CONN_EXPLAIN[mode] || "";
  }

  function dispatchStreamEvent(kind, data) {
    document.dispatchEvent(new CustomEvent("stream:" + kind, { detail: data }));
  }

  function startSSE() {
    if (CONN.pollTimer) {
      clearInterval(CONN.pollTimer);
      CONN.pollTimer = null;
    }
    setConnIndicator("connecting");

    const es = new EventSource("/api/stream");
    CONN.es = es;

    STREAM_EVENTS.forEach((kind) => {
      es.addEventListener(kind, (ev) => {
        CONN.lastEventAt = Date.now();
        CONN.failCount = 0;
        setConnIndicator("live");
        let data = null;
        try {
          data = JSON.parse(ev.data);
        } catch (e) {
          data = ev.data;
        }
        dispatchStreamEvent(kind, data);
      });
    });

    es.onopen = () => {
      CONN.lastEventAt = Date.now();
      setConnIndicator("live");
    };

    es.onerror = () => {
      CONN.failCount += 1;
      es.close();
      if (CONN.failCount >= 3) {
        // ★ 3회 연속 실패하면 3초 폴링으로 자동 폴백한다 - 프록시 환경에서
        // SSE 가 막히는 경우가 실제로 있다. 폴백이 없으면 화면이 죽는다.
        startPolling();
      } else {
        setConnIndicator("reconnecting");
        setTimeout(startSSE, 1000 * CONN.failCount);
      }
    };
  }

  function startPolling() {
    setConnIndicator("polling");
    if (CONN.pollTimer) clearInterval(CONN.pollTimer);
    CONN.pollTimer = setInterval(async () => {
      try {
        const snap = await api("/api/status");
        CONN.lastEventAt = Date.now();
        dispatchStreamEvent("tick", snap);
      } catch (e) {
        setConnIndicator("down");
      }
    }, 3000);
  }

  // 20초간 무이벤트면 (폴링 중이 아닐 때) 재연결한다.
  setInterval(() => {
    if (CONN.mode === "polling") return;
    if (CONN.lastEventAt > 0 && Date.now() - CONN.lastEventAt > 20000) {
      if (CONN.es) CONN.es.close();
      CONN.failCount = 0;
      startSSE();
    }
  }, 5000);

  // ━━ 진행 로그(접힘) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★ 최신이 맨 위. box.prepend() 로 넣고 오래된 것부터 버린다 - 장중에
  // 보고 싶은 것은 "방금 무슨 일이 있었나" 다. 아래로 쌓으면 스크롤이
  // 계속 밀린다.
  const LOG_MAX = 200;

  // ★★★ "진행 로그를 날짜로 그룹핑하고 이력 줄에는 시간만" - 날짜는 그룹 머리글로
  // 한 번만 보여주고, 각 줄은 [HH:mm:ss] 만 붙인다. 머리글과 줄이 한 상자에
  // 평평하게 섞여 있다(최신이 맨 위: 머리글 → 그 날의 줄들 → 이전 날짜 머리글...).
  const _WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

  function _dateHeader(date) {
    let label = date;
    const d = new Date(date + "T00:00:00");
    if (!isNaN(d.getTime())) label = `${date} (${_WEEKDAYS[d.getDay()]})`;
    return el("div", { class: "log-date", "data-date": date, text: label });
  }

  // "yyyy-MM-dd HH:mm:ss" (또는 ISO/유닉스 초) → { date, time }
  function _splitDateTime(v) {
    const full = _datetime(v);
    if (full === "-") return { date: "", time: "" };
    return { date: full.slice(0, 10), time: full.slice(11, 19) };
  }



  // 정적 목록(해외·암호화폐·통합)용 - 이미 최신순으로 정렬된 행을 날짜별로 묶어 붙인다.
  function appendDatedLine(box, state, date, lineEl) {
    if (date && date !== state.date) {
      box.appendChild(_dateHeader(date));
      state.date = date;
    }
    box.appendChild(lineEl);
  }


  // ★ 실시간 스트림은 "지금부터" 오는 줄만 준다 - 화면을 열었을 때 이미 쌓인 로그도 보여야
  //   "진행 로그가 안 나온다"가 없다. id 로 중복을 걸러 스트림과 겹쳐도 한 번만 찍는다.

  // ━━ 대시보드 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  const MODE_LABELS = { sim: "시뮬레이션", replay: "리플레이", web: "관찰", paper: "모의매매", live: "실거래" };

  let _dashSnapshot = null;
  let _dashChart = null;
  let _stripHistory = {}; // key -> [{t,v}]
  let _stripPrev = {};

  function _pushStripHistory(key, v) {
    const arr = _stripHistory[key] || (_stripHistory[key] = []);
    arr.push(v);
    if (arr.length > 60) arr.shift();
  }

  // ★★ 5초 폴링 주기 때문에, 정지 버튼을 눌러도 "화면이 안 바뀐다"고
  // 느끼는 문제가 있었다 - 실제로는 서버가 이미 멈췄어도 다음 5초
  // 폴링까지 화면이 그대로였다. 정지를 누르면 짧은 간격으로 몇 초간
  // 직접 확인해서, 실제로 멈추는 순간 바로 반영한다.
  async function _pollUntilStopped(statusUrl, onStopped, maxTries) {
    maxTries = maxTries || 16;
    for (let i = 0; i < maxTries; i++) {
      await new Promise((r) => setTimeout(r, 300));
      try {
        const s = await api(statusUrl);
        if (!s.running) {
          onStopped();
          return;
        }
      } catch (e) { /* 다음 시도에서 재시도 */ }
    }
  }


  function renderStrip(snap) {
    // ★★★ "매수 총액/건수, 매도 총액/건수, 익절/손절 건수, 승률,
    // 보유수/보유금액/평가손익, 그외 항목 삭제 (국내·해외·암호화폐 동일)"
    // 라는 요청에 따라, 국내주식도 다른 시장과 똑같은 공통 strip 을 쓴다.
    // 예전에 있던 중단선·거래한도·왕복비용 셀은 요청대로 뺐다.
    return renderExternalStrip({
      closed: snap.closed || [],
      positions: snap.positions || {},
    }, "domestic", "won");
  }

  // ★ 차트 눈금은 우측 64px 에 들어가야 해서 부호 + 만/억 단위로 줄여 쓴다
  //   ("+12,345원"은 안 들어간다). 정확한 금액은 마우스를 올리면 툴팁에 나온다.
  function _wonAxis(v) {
    if (v == null || isNaN(v)) return "";
    const a = Math.abs(v);
    const sign = v > 0 ? "+" : v < 0 ? "-" : "";
    if (a >= 1e8) return sign + (a / 1e8).toFixed(1) + "억";
    if (a >= 1e4) return sign + (a / 1e4).toFixed(a >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "만";
    return sign + Math.round(a).toLocaleString("ko-KR");
  }

  function _usdAxis(v) {
    if (v == null || isNaN(v)) return "";
    return (v > 0 ? "+" : v < 0 ? "-" : "") + "$" + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: v >= 100 || v <= -100 ? 0 : 1 });
  }

  const _usdTip = (v) => (v >= 0 ? "+$" : "-$") + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
  const CHART_SYMBOL_LIMIT = 6; // 선이 너무 많으면 못 읽는다 - 손익 절대값이 큰 종목 위주로 그린다.

  // 종목별 곡선(서버가 분 단위로 쌓음)을 총손익 곡선의 시각에 맞춰 같은 길이로 만든다.
  // 처음 나타나기 전 구간은 첫 값으로 채운다(그 전엔 그 종목이 없었으니 평평하다).
  function _domesticSymbolSeries(snap, times) {
    const curves = (snap && snap.symbol_curves) || {};
    const out = [];
    Object.entries(curves).forEach(([symbol, c]) => {
      const raw = c.points || [];
      if (!raw.length || !times.length) return;
      let j = 0;
      let last = raw[0][1];
      const points = times.map((t) => {
        while (j < raw.length && String(raw[j][0]) <= String(t)) { last = raw[j][1]; j++; }
        return { t, v: last };
      });
      out.push({ id: symbol, name: c.name || symbol, points });
    });
    out.sort((a, b) => Math.abs(b.points[b.points.length - 1].v) - Math.abs(a.points[a.points.length - 1].v));
    return out.slice(0, CHART_SYMBOL_LIMIT);
  }

  const DASH_CHART_OPTS = {
    height: 220, zeroBase: true, window: 30,
    yTitle: "손익(원)", xTitle: "시각",
    yAxisFormat: _wonAxis, yFormat: (v) => signed(v, "won"),
  };

  // ★ 카드·차트 노드는 한 번만 만들어 재사용한다 - 스냅샷(1초)마다 캔버스를 부수고 다시 만들면 깜빡인다.
  let _dashChartUi = null;

  function renderDashChart(snap) {
    if (!_dashChartUi || !_dashChart || _dashChart.el !== _dashChartUi.chartBox) {
      const wrap = el("div", { class: "card" });
      const head = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } });
      head.appendChild(el("div", { html: `<b>${_dashIcon("trending-up")} 손익 추이 (오늘)</b>` }));
      const seg = el("div", { class: "seg" });
      ["5분", "30분", "전체"].forEach((label, i) => {
        seg.appendChild(el("button", {
          text: label,
          class: i === 1 ? "active" : "",
          onclick: (e) => {
            $$("button", seg).forEach((b) => b.classList.remove("active"));
            e.target.classList.add("active");
            if (_dashChart) _dashChart.setWindow(i === 0 ? 5 : i === 1 ? 30 : 100000);
          },
        }));
      });
      head.appendChild(seg);
      wrap.appendChild(head);
      const chartBox = el("div");
      wrap.appendChild(chartBox);
      const hint = el("div", { class: "hint", style: { display: "none" } });
      wrap.appendChild(hint);
      if (_dashChart) _dashChart.destroy();
      _dashChart = new RealtimeChart(chartBox, DASH_CHART_OPTS);
      _dashChartUi = { wrap, chartBox, hint };
    }
    const wrap = _dashChartUi.wrap;

    const pnlPoints = (snap.pnl_curve || []).map((p) => ({ t: p.time, v: p.pnl }));
    // ★★★ "그래프에서 종목도 추가" - 총 손익 한 줄만 있어서 어느 종목이 벌고
    // 잃는지 알 수 없었다. 종목별 손익(실현+평가)을 같은 시각축에 함께 그린다.
    // 범례를 누르면 선을 껐다 켤 수 있다.
    const symbolSeries = _domesticSymbolSeries(snap, pnlPoints.map((p) => p.t));
    _dashChart.setSeries([{ id: "pnl", name: "총 손익", points: pnlPoints, bold: true }].concat(symbolSeries));
    const stopGuide = -snap.daily_loss_limit_pct * snap.allocation;
    _dashChart.setGuides({ stop: stopGuide });

    // ★★★ "매수했던 종목은 모두 보여줘" - 이 곡선은 이제 실현손익뿐
    // 아니라 보유 중인 종목의 평가손익까지 더한 값이다. 확정된 손익과
    // 섞여 보이면 오해할 수 있으니 분명히 밝힌다.
    const heldCount = Object.keys(snap.positions || {}).length;
    const hint = _dashChartUi.hint;
    if (heldCount) {
      const last = (snap.pnl_curve || [])[snap.pnl_curve.length - 1] || {};
      const realized = last.realized;
      const txt = `보유 중인 ${heldCount}종목의 평가손익까지 더한 값입니다`
        + (realized != null ? ` (이 중 확정된 손익은 ${signed(realized, "won")})` : "")
        + " - 아직 팔지 않았으므로 최종 손익은 달라질 수 있습니다.";
      if (hint.textContent !== txt) hint.textContent = txt;
      hint.style.display = "";
    } else {
      hint.style.display = "none";
    }

    return wrap;
  }








  function _hhmm(v) {
    // ★★★ "날짜 외에 매매시간을 추가해달라" - 거래 기록의 시간은 국내가
    // ISO 문자열("2026-09-09T10:00:00+09:00"), 암호화폐·해외가 유닉스 초로
    // 서로 다르다. 둘 다 받아 시:분으로 통일해서 보여준다.
    if (v == null || v === "") return "-";
    // ★ ISO 문자열은 기록된 시각을 그대로 읽는다 - Date 로 변환하면
    //   브라우저 시간대로 바뀌어, KST 로 기록된 09:35 가 다른 시각으로
    //   보일 수 있다(거래가 일어난 시각 그대로가 맞다).
    if (typeof v === "string") {
      const m = v.match(/T(\d{2}):(\d{2})/);
      if (m) return `${m[1]}:${m[2]}`;
    }
    try {
      const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
      if (isNaN(d.getTime())) return "-";
      return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    } catch (e) {
      return "-";
    }
  }

  // ★ "yyyy-MM-dd HH:mm:ss" - 거래내역·일자별·기법별·진행 로그 공용.
  // ISO 문자열은 기록된 시각을 그대로 읽고(시간대 변환 없음), 유닉스 초는
  // 브라우저 로컬 시각으로 바꾼다(_hhmm 과 같은 규칙).
  function _datetime(v) {
    if (v == null || v === "") return "-";
    if (typeof v === "string") {
      const m = v.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
      if (m) return `${m[1]} ${m[2]}:${m[3]}:${m[4]}`;
    }
    try {
      const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
      if (isNaN(d.getTime())) return "-";
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    } catch (e) {
      return "-";
    }
  }

  // 정렬용 - ISO 문자열이든 유닉스 초든 같은 척도(ms)로 바꾼다.
  function _timeValue(v) {
    if (v == null || v === "") return 0;
    if (typeof v === "number") return v * 1000;
    const t = Date.parse(v);
    return isNaN(t) ? 0 : t;
  }

  function _hhmmss(v) {
    // ★★★ "갱신 시간은 초까지만 표시해" - 예전엔 서버가 준 ISO 문자열을
    // 그대로 찍어서 "2026-09-09T15:00:00.123456+09:00" 처럼 나왔다.
    // ★ ISO 는 기록된 시각을 그대로 읽는다 - Date 로 변환하면 브라우저
    //   시간대로 바뀌어 실제 갱신 시각과 달라 보인다.
    if (v == null || v === "") return "-";
    if (typeof v === "string") {
      const m = v.match(/T(\d{2}):(\d{2}):(\d{2})/);
      if (m) return `${m[1]}:${m[2]}:${m[3]}`;
    }
    try {
      const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
      if (isNaN(d.getTime())) return "-";
      return [d.getHours(), d.getMinutes(), d.getSeconds()]
        .map((n) => String(n).padStart(2, "0")).join(":");
    } catch (e) {
      return "-";
    }
  }

  function _qty(v) {
    // ★★★ "수량은 소수점 2자리로 제한" - 암호화폐는 0.00123456 처럼 길게
    // 나와 표가 지저분했다. 정수면 소수점을 안 붙이고, 소수면 2자리까지만.
    // ★ 2자리로 자르면 0 이 되는 아주 작은 수량(0.001 BTC 등)은 예외로
    //   유효숫자를 남긴다 - "0"으로 보이면 안 산 것처럼 오해한다.
    if (v == null || v === "") return "-";
    const n = Number(v);
    if (!isFinite(n)) return "-";
    if (Number.isInteger(n)) return n.toLocaleString();
    if (n !== 0 && Math.abs(n) < 0.01) return n.toPrecision(2);
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  function usd(v) {
    // ★★★ "해외주식 매매 금액이 왜 10,714원이야?" - 해외주식 금액은
    // 달러인데 원화 포맷(won)으로 찍어서 "$10,714"가 "10,714원"으로
    // 보였다. 1,500만원어치를 산 게 맞는데 화면만 틀렸던 것이다.
    if (v == null || v === "") return "-";
    const n = Number(v);
    if (!isFinite(n)) return "-";
    return "$" + n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  // ★★★ "외화의 경우 현재 환율로 계상해서 원화로도 보여줘" - 해외주식은 전부 달러로만
  // 보여서 원화 감각으로 손익 크기를 가늠하기 어렵다는 요청. 환율은 서버(overseas_engine
  // ._usd_krw_rate())가 10분 캐시해 주는 값을 /api/overseas/status 가 status.usd_krw_rate 로
  // 실어 보낸다 - 여기서 새로 조회하지 않고, 해외주식 상태를 받을 때마다(기존 폴링 주기
  // 그대로) 갱신해 모듈 전역에 캐시해 둔다.
  let _usdKrwRate = null;

  function _captureUsdKrwRate(status) {
    if (status && status.usd_krw_rate) _usdKrwRate = Number(status.usd_krw_rate) || _usdKrwRate;
  }

  // ★★★ "모바일에서 원화표기가 잘 안보여" → "단위가 천원으로 보여주는건 모바일만"
  // 이었다가 → "아니다 모바일도 원단위로 보여줘" - 결국 모바일·PC 구분 없이 항상
  // won() 과 같은 전체 자릿수 원화로 표시한다(천원 축약은 쓰지 않는다).
  function _krwK(n) {
    return won(n);
  }
  function _krwKSigned(n) {
    return signed(n, "won");
  }

  // ★★★ "달러와 원화를 같이 표현하니 산만하네. 원화 선택해서 볼수있게해" - 달러 옆에
  // 늘 원화를 덧붙이던 방식이 오히려 산만하다는 지적. 이제 달러 전용/원화 전용 중 하나를
  // 고르는 토글(#currency-toggle, CURRENCY_KEY - 아래 다크모드 토글 옆에 있음)만 남기고,
  // "둘 다 같이" 모드는 아예 없앴다. 기본값은 "usd"(토글 없던 예전 그대로).
  function _currencyMode() {
    try { return localStorage.getItem(CURRENCY_KEY) === "krw" ? "krw" : "usd"; } catch (e) { return "usd"; }
  }

  // ── HTML 버전 - innerHTML/`html:` 로 꽂히는 곳 전용이다(예: DataGrid 의 col.fmt,
  // `el(tag,{html})`). textContent 로 꽂히는 곳엔 아래 _usdWithKrwText/_usdSignedWithKrwText
  // 를 대신 쓴다(지금은 두 버전 다 한 줄짜리 문자열만 내서 사실상 몸통이 같다).
  function _usdWithKrw(v) {
    const base = usd(v);
    if (_currencyMode() !== "krw" || base === "-" || _usdKrwRate == null) return base;
    const n = Number(v);
    if (!isFinite(n)) return base;
    // ★ "원화로 환산할때 약 은 제외해" - 근사치라는 "약" 접두어를 뺀다.
    return _krwK(n * _usdKrwRate);
  }

  // 손익처럼 부호가 붙는 달러 금액용(HTML 버전) - _usdTip 으로 부호까지 맞춘다.
  function _usdSignedWithKrw(v) {
    const base = _usdTip(v);
    if (v == null || isNaN(v) || _currencyMode() !== "krw" || _usdKrwRate == null) return base;
    return _krwKSigned(Number(v) * _usdKrwRate);
  }

  // ── 텍스트 버전 - textContent 로 값을 꽂는 곳(미니 표, 스트립 요약, DataGrid 의
  // 합계 행처럼 같은 fmt 함수가 textContent 로도 쓰이는 곳) 전용이다. "둘 다 같이"
  // 모드가 있던 예전엔 "\n"으로 두 줄을 냈지만, 이제 모드당 값이 하나뿐이라
  // HTML 버전과 결과가 완전히 같다 - 그대로 위임한다.
  function _usdWithKrwText(v) {
    return _usdWithKrw(v);
  }
  function _usdSignedWithKrwText(v) {
    return _usdSignedWithKrw(v);
  }

  function _tradeStats(status) {
    // ★★★ "매수 총액/건수, 매도 총액/건수, 익절건수, 손절건수, 승률,
    // 보유수/보유금액/평가손익" - 요청하신 지표를 청산 기록과 보유
    // 목록에서 계산한다. 세 시장(국내·해외·암호화폐)이 같은 함수를 쓴다.
    const closed = status.closed || [];
    const positions = Object.values(status.positions || {});

    // ★ 매도(청산)는 기록이 곧 매도 건수다. 매수는 "청산된 것 + 아직
    // 보유 중인 것" 둘 다 산 것이므로 합쳐야 실제 매수 건수가 된다.
    let sellAmount = 0, buyAmount = 0, closedBuy = 0, wins = 0, losses = 0, realized = 0;
    closed.forEach((r) => {
      const qty = r.qty != null ? r.qty : (r.quantity || 0);
      const entry = r.entry != null ? r.entry : (r.entry_price || 0);
      const exit = r.exit != null ? r.exit : (r.exit_price || 0);
      buyAmount += entry * qty;
      closedBuy += entry * qty;   // 청산된 것의 매수 원가 - 매도 금액과 짝을 이룬다.
      sellAmount += exit * qty;
      const pnl = r.pnl || 0;
      // ★ 실현손익 - 실제로 팔아서 확정된 손익의 합계.
      realized += pnl;
      if (pnl > 0) wins += 1;
      else if (pnl < 0) losses += 1;
    });

    let heldAmount = 0, unrealized = 0;
    positions.forEach((p) => {
      const qty = p.quantity || 0;
      const entry = p.entry_price || 0;
      heldAmount += entry * qty;
      buyAmount += entry * qty;   // ★ 보유 중인 것도 '매수한 것'이다.
      // ★ 실시간 현재가가 스냅샷에 없으면 고점을 근사치로 쓴다.
      const now = p.last_price || p.peak_price || entry;
      unrealized += (now - entry) * qty;
    });

    return {
      buyCount: closed.length + positions.length, buyAmount,
      sellCount: closed.length, sellAmount,
      // ★ 매도 금액 − 청산분 매수 원가 = 세전 손익, 거기서 수수료·세금을 뺀 것이 실현손익이다.
      //   화면에 원가와 비용을 같이 보여줘야 "매수·매도·손익 금액이 안 맞는다"가 없다.
      closedBuy, costs: (sellAmount - closedBuy) - realized,
      wins, losses, realized,
      // 성과 화면(ledger·_perfTotals)과 같은 정의 - 무승부도 거래 수에 넣는다.
      winRate: closed.length ? wins / closed.length : 0,
      heldCount: positions.length, heldAmount, unrealized,
    };
  }

  function renderExternalStrip(status, stripKey, priceUnit) {
    // ★★★ 요청대로 항목을 재구성했다 - 매수/매도 총액·건수, 익절·손절
    // 건수, 승률, 보유 현황만 남기고 나머지(감시 목록·중단선·왕복비용
    // 등)는 뺐다. 국내·해외주식·암호화폐가 모두 같은 구성을 쓴다.
    // ★★★ "매수 매도 손익 익절 손절 승률 이런 식으로 나열된 걸 그룹화" -
    // 8칸이 한 줄로 늘어서 있어 무엇끼리 관련된 값인지 한눈에 안 들어왔다.
    // 매매(매수·매도) / 성과(손익·익절·손절·승률) / 보유(보유·평가손익)
    // 세 묶음으로 나눠 각자 제목을 붙인다.
    const strip = el("div", { class: "strip-groups" });
    const groupStrips = {};
    const s = _tradeStats(status);
    // ★ "외화의 경우 현재 환율로 계상해서 원화로도 보여줘" - 해외주식 카드는 달러 옆에
    // 약식 원화 환산을 덧붙인다(_usdWithKrwText/_usdSignedWithKrwText).
    // ★ 이 셀 값은 textContent 로 꽂힌다(아래 valueEl.textContent, animateNumber 모두) -
    // <br><span> 을 쓰는 HTML 버전이 아니라 "\n" 텍스트 버전을 쓴다.
    const money = priceUnit === "usd" ? _usdWithKrwText : (v) => won(v);
    const signedMoney = priceUnit === "usd" ? _usdSignedWithKrwText : (v) => signed(v, "won");
    const count = (v) => String(Math.round(v)) + "건";

    const cells = [
      { group: "오늘 매매", key: ".buy", label: "매수", v: s.buyAmount, sub: count(s.buyCount), fmt: money },
      { group: "오늘 매매", key: ".sell", label: "매도", v: s.sellAmount, sub: `${count(s.sellCount)} · 원가 ${money(s.closedBuy)}`, fmt: money },
      // ★★★ "매수/매도 다음에 손익 추가" - 실제로 확정된 손익(매도한
      // 것들의 합계)이다. 아래 '평가손익'(아직 안 판 보유분)과는 다르다.
      { group: "오늘 성과", key: ".realized", label: "손익", v: s.realized, sub: `실현 · 비용 ${money(Math.max(0, s.costs))}`, fmt: signedMoney, tone: "dir" },
      { group: "오늘 성과", key: ".wins", label: "익절", v: s.wins, fmt: count, tone: "rise" },
      { group: "오늘 성과", key: ".losses", label: "손절", v: s.losses, fmt: count, tone: "fall" },
      { group: "오늘 성과", key: ".winrate", label: "승률", v: s.winRate, fmt: (v) => pct0(v) },
      { group: "현재 보유", key: ".held", label: "보유", v: s.heldAmount, sub: count(s.heldCount), fmt: money },
      { group: "현재 보유", key: ".unrealized", label: "평가손익", v: s.unrealized, sub: "미실현", fmt: signedMoney, tone: "dir" },
    ];

    cells.forEach((c) => {
      const cell = el("div", { class: "cell" });
      cell.appendChild(el("div", { class: "label", text: c.label }));
      let cls = "";
      if (c.tone === "dir") cls = dir(c.v);
      else if (c.tone) cls = c.v > 0 ? c.tone : "";
      const valueEl = el("div", { class: "value " + cls });
      cell.appendChild(valueEl);
      // ★★ 하단바(스파크라인) 위치를 모든 셀에서 일정하게 맞춘다 - 예전엔
      // sub 가 있는 셀만 한 줄이 더 생겨서 셀마다 그래프 높이가 들쭉날쭉
      // 했다. sub 가 없어도 같은 높이의 빈 자리를 둔다.
      cell.appendChild(el("div", { class: "label sub", text: c.sub || "\u00a0" }));
      const spark = el("canvas", { class: "spark" });
      cell.appendChild(spark);
      if (!groupStrips[c.group]) {
        const box = el("div", { class: "strip-group" });
        box.appendChild(el("div", { class: "strip-group-title", text: c.group }));
        const inner = el("div", { class: "strip strip-wide strip-in-group" });
        box.appendChild(inner);
        strip.appendChild(box);
        groupStrips[c.group] = inner;
      }
      groupStrips[c.group].appendChild(cell);

      const histKey = stripKey + c.key;
      _pushStripHistory(histKey, c.v);
      const prev = _stripPrev[histKey];
      if (prev == null) {
        valueEl.textContent = c.fmt(c.v);
      } else {
        animateNumber(valueEl, prev, c.v, { fmt: c.fmt });
      }
      _stripPrev[histKey] = c.v;

      requestAnimationFrame(() => sparkline(spark, _stripHistory[histKey], { fill: true }));
    });

    // 그룹마다 셀 수가 달라(2·4·2) 폭을 셀 수에 비례해 나눈다.
    Object.values(groupStrips).forEach((inner) => {
      const n = inner.children.length;
      inner.style.setProperty("--cols", String(n));
      inner.parentElement.style.flex = String(n);
    });

    return strip;
  }

  function renderExternalLogConsole(closedRows, idKey, priceUnit) {
    // ★ 국내 진행 로그(.console)와 같은 스타일 - 암호화폐·해외주식은
    // 실시간 로그 스트림이 없으니, 최근 청산 기록을 로그 형태로 대신
    // 보여준다. 국내주식 화면에 "로그가 있다"는 인상과 맞춘다.
    const details = el("details", { open: true });
    details.appendChild(el("summary", { html: `${_dashIcon("clock")} 진행 로그 (최근 청산)` }));
    const box = el("div", { class: "console" });
    const sorted = (closedRows || []).slice().sort((a, b) => (b.exit_time || 0) - (a.exit_time || 0));
    if (!sorted.length) {
      box.appendChild(el("div", { class: "line", text: "아직 기록이 없습니다." }));
    } else {
      const dstate = { date: null };
      sorted.slice(0, LOG_MAX).forEach((r) => {
        const dt = _splitDateTime(r.exit_time);
        const time = dt.time;
        // ★ 로그 한 줄 문장 안에 끼워 넣는 값이라 textContent 로 꽂힌다(el text:) - 텍스트 버전.
        const pnlText = r.pnl != null ? (priceUnit === "usd" ? _usdSignedWithKrwText(r.pnl) : signed(r.pnl, "won")) : "-";
        const cls = (r.pnl || 0) > 0 ? "gain" : (r.pnl || 0) < 0 ? "loss" : "";
        appendDatedLine(box, dstate, dt.date, el("div", {
          class: "line " + cls,
          text: `[${time}] ${r[idKey]} 청산 · ${r.reason || ""} · 손익 ${pnlText}`,
        }));
      });
    }
    details.appendChild(box);
    return details;
  }

  const _externalCharts = {};  // key -> RealtimeChart 인스턴스(암호화폐/해외주식/통합 각자 독립)

  const _externalChartUi = {};  // key -> {wrap, chartBox, hint, build, total} - 노드를 재사용해 깜빡임을 막는다.

  function renderExternalPnlChart(closedRows, chartKey, positions) {
    let ui = _externalChartUi[chartKey];
    if (!ui || !_externalCharts[chartKey] || _externalCharts[chartKey].el !== ui.chartBox) {
      const wrap = el("div", { class: "card" });
      const head = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "var(--s2)" } });
      head.appendChild(el("div", { html: `<b>${_dashIcon("trending-up")} 손익 추이 (오늘)</b>` }));
      const seg = el("div", { class: "seg" });
      wrap.appendChild(head);
      const chartBox = el("div");
      wrap.appendChild(chartBox);
      const hint = el("div", { class: "hint", style: { display: "none" } });
      wrap.appendChild(hint);
      ui = { wrap, chartBox, hint, seg, build: null, total: 1 };
      const isUsd0 = chartKey === "overseas";
      if (_externalCharts[chartKey]) _externalCharts[chartKey].destroy();
      _externalCharts[chartKey] = new RealtimeChart(chartBox, {
        height: 180, zeroBase: true, window: 100000,
        // ★ 이 차트의 x 는 시간에 비례하지 않고 "청산이 일어난 순서"다.
        yTitle: isUsd0 ? "손익($)" : chartKey === "unified" ? "손익(합산)" : "손익(원)",
        xTitle: "청산 시각",
        yAxisFormat: isUsd0 ? _usdAxis : _wonAxis,
        yFormat: isUsd0 ? _usdTip : (v) => signed(v, "won"),
      });
      // ★ 5분/30분/전체 창 버튼 - 여기서는 "청산 건수" 기준(최근 N건/전체). 최신 데이터는 ui.build/ui.total 로 본다.
      ["최근 5건", "최근 30건", "전체"].forEach((label, i) => {
        seg.appendChild(el("button", {
          text: label, class: i === 2 ? "active" : "",
          onclick: (e) => {
            $$("button", seg).forEach((b) => b.classList.remove("active"));
            e.target.classList.add("active");
            ui.win = i === 0 ? 5 : i === 1 ? 30 : 0;
            _externalCharts[chartKey].setSeries(ui.build(ui.win || ui.total));
          },
        }));
      });
      head.appendChild(seg);
      _externalChartUi[chartKey] = ui;
    }
    const wrap = ui.wrap;
    const chartBox = ui.chartBox;

    const sorted = (closedRows || []).filter((r) => r.exit_time).slice().sort((a, b) => a.exit_time - b.exit_time);
    let cum = 0;
    const points = sorted.map((r) => {
      cum += r.pnl || 0;
      return { t: r.exit_time * 1000, v: cum };
    });

    // ★★★ "매수했던 종목은 모두 보여줘" - 예전엔 청산된 거래만 그려서,
    // 아직 보유 중인 종목(= 매수했지만 안 판 것)이 그래프에서 통째로
    // 빠져 있었다. 실현손익만 보이니 "지금 내 손익이 얼마인지"를 알 수
    // 없었다. 보유분의 평가손익을 마지막 점으로 이어 붙여 전체 흐름을
    // 보여준다(실현 + 미실현).
    const heldEntries = Object.entries(positions || {});
    const held = heldEntries.map(([, p]) => p);
    const unrealByKey = {};
    let unrealized = 0;
    heldEntries.forEach(([key, p]) => {
      const qty = p.quantity || 0;
      const entry = p.entry_price || 0;
      const now = p.last_price || p.peak_price || entry;
      unrealByKey[key] = (now - entry) * qty;
      unrealized += unrealByKey[key];
    });
    const nowT = Date.now();
    if (held.length) {
      points.push({ t: nowT, v: cum + unrealized });
    }

    // ★★★ "그래프에서 종목도 추가" - 총 누적 손익 한 줄뿐이라 어느 종목이
    // 벌고 잃는지 알 수 없었다. 청산 이벤트(+지금 시점)마다 종목별 누적
    // 손익을 찍어 총합과 같은 시각축에 겹쳐 그린다(길이가 같아야 x축이 맞는다).
    const symKey = (r) => r.symbol || r.market;
    const labelOf = {};
    sorted.forEach((r) => { const k = symKey(r); if (k) labelOf[k] = r._label || r.name || k; });
    heldEntries.forEach(([k, p]) => { if (!labelOf[k]) labelOf[k] = p.name || k; });
    const symKeys = Object.keys(labelOf);
    const symRun = {};
    const symPts = {};
    symKeys.forEach((k) => { symRun[k] = 0; symPts[k] = []; });
    sorted.forEach((r) => {
      const k = symKey(r);
      if (k) symRun[k] += r.pnl || 0;
      symKeys.forEach((kk) => symPts[kk].push({ t: r.exit_time * 1000, v: symRun[kk] }));
    });
    if (held.length) {
      symKeys.forEach((kk) => symPts[kk].push({ t: nowT, v: symRun[kk] + (unrealByKey[kk] || 0) }));
    }
    const symbolSeriesAll = symKeys
      .filter((k) => symPts[k].length)
      .map((k) => ({ id: k, name: labelOf[k], points: symPts[k] }))
      .sort((a, b) => Math.abs(b.points[b.points.length - 1].v) - Math.abs(a.points[a.points.length - 1].v))
      .slice(0, CHART_SYMBOL_LIMIT);
    const buildSeries = (n) => [{ id: "pnl", name: "누적 손익", points: points.slice(-n), bold: true }]
      .concat(symbolSeriesAll.map((s) => ({ ...s, points: s.points.slice(-n) })));

    const isUsd = chartKey === "overseas";

    // ★★★ "모든 대시보드는 국내주식처럼 그래프가 포함되어야 한다" -
    // 청산 기록이 아직 없다고 차트 자체를 안 그리면 이 화면만 그래프가
    // 빠진 것처럼 보인다. 데이터가 없어도 빈 차트(0선)를 항상 그리고,
    // 그 밑에 안내만 덧붙인다 - 국내주식 손익추이 차트와 같은 대우다.
    const chart = _externalCharts[chartKey];
    ui.build = buildSeries;
    ui.total = points.length || 1;
    chart.setSeries(buildSeries(ui.win || ui.total));
    let hintText = "";
    if (!points.length) {
      hintText = "아직 매매 기록이 없습니다 - 매수·청산이 쌓이면 이 위에 그려집니다.";
    } else if (held.length) {
      // ★ 마지막 점이 "지금 팔면 얼마인지"라는 걸 분명히 알린다 - 확정된 손익과 섞여 보이면 오해할 수 있다.
      hintText = `마지막 점은 보유 중인 ${held.length}종목의 평가손익(${signed(unrealized, "won")})까지 더한 값입니다`
        + " - 아직 팔지 않았으므로 확정된 손익이 아닙니다.";
    }
    if (ui.hint.textContent !== hintText) ui.hint.textContent = hintText;
    ui.hint.style.display = hintText ? "" : "none";
    return wrap;
  }







  // ★ 글자가 잘려 가로로 넘치는 영역(진행 로그·표·탭 버튼 줄)은 마우스로 끌어서 옆으로 볼 수 있다.
  //   터치 화면은 브라우저 기본 스와이프가 이미 되므로 마우스(포인터 종류 mouse)만 다룬다.
  function _enableDragScroll() {
    const SEL = ".console, .mini-table-wrap, .seg, .filter-row, .hscroll";
    let drag = null;
    const scroller = (node) => {
      for (let n = node; n && n !== document.body; n = n.parentElement) {
        if (n.matches && n.matches(SEL) && n.scrollWidth > n.clientWidth + 1) return n;
      }
      return null;
    };
    document.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "mouse" || e.button !== 0) return;
      if (e.target.closest("button, a, input, select, textarea, summary")) return;
      const sc = scroller(e.target);
      if (!sc) return;
      drag = { sc, x: e.clientX, left: sc.scrollLeft, moved: false };
    });
    document.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x;
      if (!drag.moved && Math.abs(dx) < 4) return;
      if (!drag.moved) { drag.moved = true; drag.sc.classList.add("dragging"); }
      drag.sc.scrollLeft = drag.left - dx;  // 왼쪽으로 끌면 오른쪽 내용이 보인다
    });
    const end = () => {
      if (!drag) return;
      const { sc, moved } = drag;
      drag = null;
      sc.classList.remove("dragging");
      if (moved) {
        // 끌기가 끝날 때 딸려오는 클릭(행 선택 등)은 막는다.
        const stop = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
        window.addEventListener("click", stop, { capture: true, once: true });
        setTimeout(() => window.removeEventListener("click", stop, { capture: true }), 0);
      }
    };
    document.addEventListener("pointerup", end);
    document.addEventListener("pointercancel", end);
    // 넘치는 영역에만 손 모양 커서를 붙인다.
    document.addEventListener("pointerover", (e) => {
      if (e.pointerType !== "mouse") return;
      const sc = scroller(e.target);
      if (sc) sc.classList.add("drag-x");
    });
  }
  _enableDragScroll();

  // ━━ 종목 시세 + 매수·매도 시점 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // 손익 곡선만으로는 "그때 시세가 어땠고 어디서 사고팔았는지"를 알 수 없다 - 종목을 골라 분봉 시세 위에
  // 매수(▲)·매도(▼) 시점을 얹고, "상세보기"로 그래프만 크게 띄운다. 세 시장(국내·해외·암호화폐)이 같은 부품을 쓴다.
  const _symCharts = {};  // key -> {card, update(options)}

  function _chartUrl(key, symbol, count) {
    const q = "?count=" + count;
    return key === "domestic"
      ? "/api/chart/" + encodeURIComponent(symbol) + q
      : "/api/market-chart/" + key + "/" + encodeURIComponent(symbol) + q;
  }

  // 손절·목표선은 최근 매수가 기준(국내만 서버가 비율을 준다).
  function _chartGuides(data) {
    if (data.stop_pct == null || data.target_pct == null) return null;
    const buys = (data.marks || []).filter((m) => m.kind === "buy");
    if (!buys.length) return null;
    const entry = buys[buys.length - 1].price;
    return { stop: entry * (1 - data.stop_pct), target: entry * (1 + data.target_pct) };
  }

  // 고를 수 있는 종목: 보유 → 오늘 매매한 종목 → 감시 목록 순(중복 제거).
  function _chartSymbols(key, status, closedToday) {
    const idKey = key === "crypto" ? "market" : "symbol";
    const out = [];
    const seen = new Set();
    const add = (id, name, tag) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      out.push({ value: id, label: tag + (name && name !== id ? name + " (" + id + ")" : id) });
    };
    Object.entries(status.positions || {}).forEach(([id, p]) => add(id, p && p.name, "● "));
    (closedToday || []).slice().reverse().forEach((r) => add(r[idKey], r.name, "✓ "));
    const watch = key === "domestic"
      ? (status.candidates || []).map((c) => [c.symbol, c.name])
      : (status.watchlist || []).map((id) => [id, ""]);
    watch.forEach(([id, name]) => add(id, name, ""));
    return out;
  }

  // 차트 하나를 container 에 그린다(카드·팝업 공용). 이미 그려져 있으면 데이터만 바꿔 줌·이동 상태를 지킨다.
  async function _drawSymbolChart(key, symbol, container, opts) {
    const data = await api(_chartUrl(key, symbol, opts.count || 180));
    if (!data.bars || !data.bars.length) {
      if (opts.state.chart) { opts.state.chart.destroy(); opts.state.chart = null; }
      fill(container, el("div", { class: "hint", text: "이 종목의 분봉 시세를 받지 못했습니다. 잠시 뒤 다시 시도됩니다." }));
      return null;
    }
    if (!opts.state.chart) {
      container.innerHTML = "";
      const box = el("div");
      container.appendChild(box);
      opts.state.chart = new CandleChart(box, { height: opts.height, unit: data.unit || (key === "overseas" ? "usd" : "won") });
    }
    opts.state.chart.setData({ bars: data.bars, marks: data.marks || [], guides: _chartGuides(data), unit: data.unit });
    return data;
  }

  function _chartNote(d) {
    const marks = d.marks || [];
    const b = marks.filter((m) => m.kind === "buy").length;
    const sl = marks.filter((m) => m.kind === "sell").length;
    return marks.length ? "▲ 매수 " + b + "회 · ▼ 매도 " + sl + "회" : "이 구간에는 매매 기록이 없습니다";
  }

  // 상세보기: 그래프만 크게(팝업). 열려 있는 동안 15초마다 새로 받고, 닫히면 정리한다.
  function _openSymbolChartModal(key, symbol, label) {
    const body = el("div", { style: { whiteSpace: "normal" } });
    const holder = el("div");
    const note = el("div", { class: "hint", style: { marginTop: "6px" } });
    body.appendChild(holder);
    body.appendChild(note);
    const state = { chart: null };
    const height = Math.max(320, Math.round(window.innerHeight * 0.62));
    const shut = openModal("🔎 " + label + " · 시세와 매매 시점", body);
    const box = body.closest(".modal-box");
    if (box) box.classList.add("wide");
    async function load() {
      try {
        const d = await _drawSymbolChart(key, symbol, holder, { height, state, count: 200 });
        if (d) note.textContent = _chartNote(d) + " · 휠/핀치로 확대·축소, 드래그로 이동, 더블클릭으로 전체";
      } catch (e) {
        if (!state.chart) fill(holder, el("div", { class: "hint fall", text: "불러오지 못했습니다: " + e.message }));
      }
    }
    load();
    const timer = setInterval(() => {
      if (!body.isConnected) { clearInterval(timer); if (state.chart) state.chart.destroy(); return; }
      load();
    }, 15000);
    return shut;
  }

  function _buildSymbolChartCard(key) {
    const card = el("div", { class: "card" });
    const head = el("div", { class: "ctl-head" });
    head.appendChild(el("b", { html: `${_dashIcon("chart")} 종목 시세 · 매매 시점` }));
    head.appendChild(infoIcon("● 보유 중 · ✓ 오늘 매매한 종목 · 그 밖은 감시 목록입니다.\n분봉 시세 위에 매수(▲)·매도(▼) 시점을 표시합니다. 봉에 마우스를 올리면 자세히 보입니다."));
    const select = el("select", { class: "sym-select" });
    const detail = el("button", { class: "b small", html: `${_dashIcon("search")} 상세보기` });
    detail.disabled = true;
    const actions = el("div", { class: "ctl-actions" });
    actions.appendChild(select);
    actions.appendChild(detail);
    head.appendChild(actions);
    card.appendChild(head);
    const holder = el("div");
    const note = el("div", { class: "hint" });
    card.appendChild(holder);
    card.appendChild(note);

    const st = { chart: null, symbol: "", sig: "", labels: {} };
    async function load() {
      if (!st.symbol) {
        if (st.chart) { st.chart.destroy(); st.chart = null; }
        fill(holder, el("div", { class: "hint", text: "표시할 종목이 없습니다 - 보유하거나 매매한 종목·감시 종목이 생기면 고를 수 있습니다." }));
        note.textContent = "";
        return;
      }
      const sym = st.symbol;
      try {
        const d = await _drawSymbolChart(key, sym, holder, { height: 260, state: st });
        if (sym !== st.symbol) return;
        note.textContent = d ? _chartNote(d) : "";
      } catch (e) {
        if (!st.chart) fill(holder, el("div", { class: "hint", text: "시세를 불러오지 못했습니다: " + e.message }));
      }
    }
    select.onchange = () => {
      st.symbol = select.value;
      if (st.chart) { st.chart.destroy(); st.chart = null; }  // 종목이 바뀌면 줌 상태도 새로
      load();
    };
    detail.onclick = () => { if (st.symbol) _openSymbolChartModal(key, st.symbol, st.labels[st.symbol] || st.symbol); };
    // 보이는 동안 주기적으로 새로 받는다(탭이 숨겨져 있으면 건너뜀).
    setInterval(() => {
      if (document.hidden || !card.isConnected || card.offsetParent === null) return;
      load();
    }, 20000);

    const handle = {
      card,
      update(options) {
        const sig = options.map((o) => o.value + "|" + o.label).join("\n");
        if (sig === st.sig) return;
        st.sig = sig;
        st.labels = {};
        options.forEach((o) => { st.labels[o.value] = o.label.replace(/^[●✓] /, ""); });
        select.innerHTML = "";
        options.forEach((o) => select.appendChild(el("option", { value: o.value, text: o.label })));
        detail.disabled = !options.length;
        if (options.some((o) => o.value === st.symbol)) { select.value = st.symbol; return; }
        st.symbol = options.length ? options[0].value : "";
        select.value = st.symbol;
        if (st.chart) { st.chart.destroy(); st.chart = null; }
        load();
      },
    };
    _symCharts[key] = handle;
    return handle;
  }

  // ━━ 시장 공통 대시보드 부품 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★★★ "통합·국내·해외·암호화폐의 내용과 항목이 다르다 - 통일하고 UI 를 더 깔끔하고
  // 간략하게" - 시장마다 따로 만들던 카드(자동매매 상태·보유·감시)를 한 벌의 부품으로
  // 합쳤다. 네 탭이 같은 순서(상태 → 지표 → 손익 추이 → 보유/감시 → 진행 로그)와
  // 같은 항목을 쓰고, 시장별 차이는 데이터(통화·모드 이름)로만 드러난다.

  const MARKETS = {
    domestic: { key: "domestic", emoji: "🇰🇷", name: "국내주식", unit: "won" },
    overseas: { key: "overseas", emoji: "🌍", name: "해외주식", unit: "usd" },
    crypto: { key: "crypto", emoji: "🪙", name: "암호화폐", unit: "won" },
    swing: { key: "swing", emoji: "📈", name: "스윙", unit: "won" },
  };
  // ★ 해외주식(unit "usd")만 달러 옆에 원화 환산을 덧붙인다 - 국내·암호화폐·스윙은
  // 이미 원화라 그대로 둔다(_usdWithKrwText/_usdSignedWithKrwText 참고).
  // ★ 아래 두 헬퍼(_moneyOf/_signedMoneyOf)의 호출부는 거의 전부 미니 표(_miniTable)나
  // el(..., {text}) 처럼 textContent 로 값을 꽂는 곳이라 기본은 텍스트("\n") 버전을 쓴다.
  // DataGrid 의 col.fmt 처럼 innerHTML 로 꽂히는(그리고 같은 fmt 가 합계 행에서
  // textContent 로도 재사용되지 않는) 소수의 자리에서만 아래 HTML 버전을 쓴다.
  const _moneyOf = (unit) => (unit === "usd" ? _usdWithKrwText : won);
  const _signedMoneyOf = (unit) => (unit === "usd" ? _usdSignedWithKrwText : (v) => signed(v, "won"));
  const _signedMoneyOfHtml = (unit) => (unit === "usd" ? _usdSignedWithKrw : (v) => signed(v, "won"));
  const _MODE_NAMES = { web: "관찰", sim: "시뮬레이션", replay: "리플레이", paper: "모의매매", live: "실거래" };

  function _modeText(status) {
    return _MODE_NAMES[status.mode] || (status.is_live ? "실거래" : status.mode ? String(status.mode) : "-");
  }

  function _heldCount(status) {
    const p = status.positions;
    return typeof p === "number" ? p : Object.keys(p || {}).length;
  }

  // 가벼운 표 - DataGrid(검색·정렬·페이지) 는 이 작은 목록에는 과하다.
  function _miniTable(columns, rows, onRowClick) {
    const table = el("table", { class: "mini-table" });
    const head = el("tr");
    columns.forEach((c) => head.appendChild(el("th", { class: c.cls || "", text: c.label })));
    table.appendChild(el("thead", {}, [head]));
    const body = el("tbody");
    rows.forEach((r) => {
      const tr = el("tr");
      columns.forEach((c) => {
        const td = el("td", { class: c.cls || "" });
        const v = c.render(r);
        if (v instanceof Node) td.appendChild(v);
        else td.textContent = v == null ? "" : String(v);
        tr.appendChild(td);
      });
      if (onRowClick) {
        tr.classList.add("clickable");
        tr.addEventListener("click", () => onRowClick(r));
      }
      body.appendChild(tr);
    });
    table.appendChild(body);
    // ★ table-wrap(가로 스크롤)·scroll-box(세로 높이 제한) 는 공용 디자인 계약 클래스 -
    //   후보·보유 목록이 길어져도 패널 전체가 한없이 늘어나지 않는다.
    return el("div", { class: "mini-table-wrap table-wrap scroll-box" }, [table]);
  }

  // positions 객체({id: 포지션}) → 화면용 행. 국내·해외·암호화폐가 같은 모양이 된다.
  // ★★★ "종목선정 기법과 매매기법이 다르니 단타매매 대상으로 선정되도 스윙매매 대상이 될 수
  // 있어" - 스윙은 국내·해외·암호화폐 포지션을 한 엔진에서 함께 들고 있어서, 통화(단위)를
  // marketKey("swing")가 아니라 각 포지션 자체의 p.market(실제 자산 시장)으로 정해야 한다
  // (해외 스윙 포지션을 원화로 잘못 표시하는 것을 막는다). 국내·해외·암호화폐 탭은 p.market 이
  // 없으니 그대로 marketKey 를 쓴다.
  function _positionRows(marketKey, positions) {
    return Object.entries(positions || {}).map(([id, p]) => {
      const assetMarket = p.market && MARKETS[p.market] ? p.market : marketKey;
      const unit = MARKETS[assetMarket].unit;
      const qty = Number(p.quantity) || 0;
      const entry = Number(p.entry_price) || 0;
      // 실시간 현재가가 스냅샷에 없으면 고점(peak_price)으로 근사한다.
      const last = Number(p.last_price || p.peak_price || entry) || entry;
      return {
        market: marketKey, assetMarket, unit, id, name: p.name || id, qty, entry, last,
        pnl: (last - entry) * qty, pct: entry ? (last - entry) / entry : 0, tech: p.technique || "",
        adds: Number(p.adds) || 0, scaledOut: Number(p.scaled_out) || 0,
      };
    });
  }

  function renderHoldingsCard(rows, opts) {
    opts = opts || {};
    const card = el("div", { class: "card" });
    const h = el("h2", { html: `${_dashIcon("wallet")} 보유 (${rows.length})` });
    h.appendChild(infoIcon("평가손익은 현재가를 받지 못한 종목은 고점 기준으로 어림한 값입니다."));
    card.appendChild(h);
    if (!rows.length) {
      card.appendChild(el("div", { class: "hint", text: "보유 중인 종목이 없습니다." }));
      return card;
    }
    const cols = [];
    if (opts.showMarket) cols.push({ label: "", cls: "mk", render: (r) => MARKETS[r.assetMarket || r.market].emoji });
    cols.push({
      label: "종목",
      // 추가 매수(＋n)·분할 매도(－n) 진행 상태를 종목 이름 옆에 작게 붙인다.
      render: (r) => el("span", {
        text: r.name + (r.adds ? ` ＋${r.adds}` : "") + (r.scaledOut ? ` －${r.scaledOut}` : ""),
        title: (r.name !== r.id ? `${r.name} (${r.id})` : r.id)
          + (r.adds ? ` · 추가 매수 ${r.adds}회` : "") + (r.scaledOut ? ` · 분할 매도 ${r.scaledOut}회` : ""),
      }),
    });
    cols.push({ label: "수량", cls: "num", render: (r) => _qty(r.qty) });
    cols.push({ label: "진입가", cls: "num", render: (r) => _moneyOf(r.unit)(r.entry) });
    // 현재가(시세를 못 받은 종목은 고점으로 어림한 값 - 평가손익과 같은 기준)
    cols.push({ label: "현재가", cls: "num", render: (r) => _moneyOf(r.unit)(r.last) });
    cols.push({
      label: "평가손익", cls: "num",
      render: (r) => el("span", {
        class: dir(r.pnl),
        text: `${_signedMoneyOf(r.unit)(r.pnl)} (${pct(r.pct)})`,
      }),
    });
    cols.push({ label: "기법", cls: "col-tech", render: (r) => el("span", { html: r.tech ? techBadgeHTML(r.tech, { label: r.tech }) : "-" }) });
    card.appendChild(_miniTable(cols, rows, opts.onRow));
    return card;
  }

  function renderWatchCard(rows, opts) {
    opts = opts || {};
    const card = el("div", { class: "card" });
    const h = el("h2", { html: `${_dashIcon("target")} ${esc(opts.title || "감시")} (${rows.length})` });
    if (opts.help) h.appendChild(infoIcon(opts.help));
    card.appendChild(h);
    if (!rows.length) {
      card.appendChild(el("div", { class: "hint", text: opts.empty || "지금 감시 중인 종목이 없습니다." }));
      return card;
    }
    const cols = [];
    if (opts.showMarket) cols.push({ label: "", cls: "mk", render: (r) => MARKETS[r.market].emoji });
    cols.push({
      label: "종목",
      render: (r) => el("span", { text: r.name, title: r.sub ? `${r.name} · ${r.sub}` : r.name }),
    });
    cols.push({
      label: "현재가", cls: "num",
      render: (r) => (r.price == null ? "-" : el("span", {
        class: dir(r.change || 0),
        text: `${_moneyOf(MARKETS[r.market].unit)(r.price)}${r.change != null ? ` (${pct(r.change)})` : ""}`,
      })),
    });
    cols.push({ label: "상태", render: (r) => r.status || "" });
    card.appendChild(_miniTable(cols, rows, opts.onRow));
    return card;
  }

  // 감시 행: 국내는 서버가 고른 후보, 해외·암호화폐는 관심 목록 중 아직 안 산 것.
  function _watchRowsFromCandidates(candidates) {
    return (candidates || []).map((c) => ({
      market: "domestic", id: c.symbol, name: c.name || c.symbol, sub: c.theme,
      price: c.last_price, change: c.change_rate, status: c.status || "관찰 중",
    }));
  }

  function _watchRowsFromWatchlist(marketKey, status) {
    const held = status.positions || {};
    return (status.watchlist || []).filter((w) => !held[w]).map((w) => ({
      market: marketKey, id: w, name: w, price: null, change: null, status: "신호 대기",
    }));
  }

  // 스윙 감시 행 - 매일 정규장이 끝난 뒤 한 번 뽑은(추세 필터를 통과한) 후보 중 아직 안 산 것.
  // ★ 국내·해외·암호화폐 후보가 섞여 있으므로(c.market) 종목마다 실제 자산 시장 아이콘을 쓴다.
  function _watchRowsFromSwingCandidates(status) {
    const held = status.positions || {};
    return (status.candidates || []).filter((c) => !held[c.symbol]).map((c) => ({
      market: (c.market && MARKETS[c.market]) ? c.market : "domestic",
      id: c.symbol, name: c.name || c.symbol, sub: c.theme,
      price: null, change: null, status: "신호 대기",
    }));
  }

  function _watchHelp(marketKey, status) {
    if (marketKey === "domestic") {
      return "거래대금·테마 스크리닝으로 고른 후보입니다. 선정 근거는 [종목 선정] 화면에서 봅니다.";
    }
    if (marketKey === "swing") {
      return "매일 정규장이 끝난 뒤 한 번, 국내·해외·암호화폐 세 시장의 테마 후보 + 스윙 관심 종목 중 "
        + "중기 상승 추세(이동평균 위)이면서 최근 1주일간 실제로 오른 종목만 다시 고릅니다 - "
        + "당일 실시간 등락률로 고르는 단타와 다른 기준입니다.";
    }
    if (marketKey === "crypto") {
      return status.auto_top_volume
        ? `전날 거래대금 상위 ${status.top_volume_count || 10}종목(스테이블코인 제외)만 감시합니다. 매일 자동으로 다시 뽑고, 시작 전에는 기본 목록이 보입니다.`
        : "미리 정한 감시 목록에서 신호를 기다립니다.";
    }
    if (status.auto_select && status.running) {
      return "매일 거래대금·급등 상위 종목을 자동으로 다시 뽑은 오늘의 감시 목록입니다.";
    }
    if (status.auto_select) {
      return "종목 자동 선정이 켜져 있습니다 - 시작하면 오늘의 상위 종목을 다시 뽑습니다. 그 전에는 마지막 목록을 보여줍니다.";
    }
    return "오늘의 미국 테마주와 직접 추가한 관심 종목에서 신호를 기다립니다. 선정 근거는 [종목 선정] 화면에서 봅니다.";
  }

  function _domesticEmptyReason(snap) {
    // ★ "후보가 없습니다"만 있으면 시장이 조용한지, 시세를 못 받았는지, 진입 시간이 끝났는지
    //   알 수 없다 - 이유를 우선순위대로 보여준다.
    const ses = (snap && snap.session) || {};
    if (ses.trading === false && ses.why) {
      return `${ses.label || ""}: ${ses.why}`
        + (ses.scan_start && ses.scan_end ? ` (신규 진입 구간 ${ses.scan_start}~${ses.scan_end})` : "");
    }
    return (snap && snap.selection && snap.selection.summary) || "지금은 조건을 충족하는 후보가 없습니다.";
  }

  function _goSelection(row) {
    if (row.market !== "domestic") return;
    window.APP.showTab("selection");
    window.APP.focusCandidate(row.id);
  }

  // ★ "과다거래 의심" 배너는 없앴다 - 관심 종목이 적은 시장(암호화폐 등)에서는 같은 종목을
  // 하루에 여러 번 사고파는 것 자체가 정상 동작이라, 이 배너가 항상 오탐(false positive)만
  // 냈다("당연히 같은 종목을 여러 번 할 수 있어. 이런 메시지는 불필요해." - 사용자 피드백).

  // 한 줄 요약 - 값이 있는 항목만 이어 붙인다.
  function _controlSummary(m, s, running) {
    if (!running && s.positions === undefined && s.cash == null) return "";
    const parts = [];
    if (s.cash != null) parts.push(`현금 ${_moneyOf(m.unit)(s.cash)}`);
    parts.push(`보유 ${_heldCount(s)}종`);
    const today = s.today_trades != null ? s.today_trades : s.trades;
    if (today != null) parts.push(`오늘 ${today}건`);
    if (s.closed_count != null) parts.push(`청산 누적 ${s.closed_count}건`);
    return parts.join(" · ");
  }

  const _controlTimers = {};

  // 국내·해외·암호화폐 자동매매 상태 카드 한 벌 - 예전엔 세 함수가 조금씩 다른 항목을 그렸다.
  // o: { market, statusUrl, startUrl, stopUrl, onStatus(status), offHelp(status), immediateModes }
  function _makeControlCard(o) {
    const m = MARKETS[o.market];
    const card = el("div", { class: "card ctl" });
    const body = el("div");
    card.appendChild(body);

    let latest = null;  // 화면 내용이 같으면 노드를 다시 만들지 않으므로, 핸들러는 항상 최신 상태를 본다.

    async function start(refresh) {
      const post = (b) => api(o.startUrl, { method: "POST", body: b });
      try {
        await post({});
        toast(`${m.name} 자동매매를 시작했습니다.`);
      } catch (e) {
        if (!(e.message && e.message.includes("실매매"))) throw e;
        const typed = prompt("실거래를 시작합니다. '실매매' 를 입력하세요.") || "";
        await post({ confirm: typed });
        toast(`${m.name} 자동매매(실거래)를 시작했습니다.`);
      }
      refresh();
    }

    async function refresh() {
      // ★ 5초마다 도는 폴링이라 문서 스크롤을 보존해 둔다(fill() 이 내용이
      // 같으면 건드리지 않지만, 상태가 실제로 바뀌면 이 카드 영역을 다시
      // 그린다 - try/finally 로 아래 두 return 경로 모두 커버한다).
      const _scrollEl = _scrollAnchorEl();
      const _savedTop = _scrollEl.scrollTop;
      try {
      let status;
      try {
        status = await api(o.statusUrl);
        if (o.onStatus) o.onStatus(status);
      } catch (e) {
        body.innerHTML = "";
        body.appendChild(el("div", { class: "hint", text: "상태를 불러오지 못했습니다." }));
        return;
      }
      const out = el("div");
      latest = status;
      const running = !!status.running;
      const held = _heldCount(status);

      const head = el("div", { class: "ctl-head" });
      head.appendChild(el("b", { text: `${m.emoji} ${m.name} 자동매매` }));
      head.appendChild(el("span", {
        class: "ctl-badge " + (running ? "on" : "off"),
        text: running ? `${_modeText(status)} 중` : `꺼짐 · ${_modeText(status)}`,
      }));
      if (!running && o.offHelp) head.appendChild(infoIcon(o.offHelp(status)));

      const actions = el("div", { class: "ctl-actions" });
      if (!running) {
        actions.appendChild(el("button", {
          class: "b", html: `${_dashIcon("play")} 자동매매 시작`,
          onclick: () => confirmPassword(async () => {
            try {
              busy(card, true);
              await start(refresh);
            } catch (e) {
              toast(e.message, "error");
            } finally {
              busy(card, false);
            }
          }, { title: `🔒 ${m.name} 자동매매 시작`, hint: "시작하려면 비밀번호를 다시 입력하세요." }),
        }));
      } else {
        actions.appendChild(el("button", {
          class: "b quiet-danger", html: `${_dashIcon("pause")} 정지`,
          onclick: () => confirmPassword(async () => {
            try {
              await api(o.stopUrl, { method: "POST", body: { close_positions: false } });
              const immediate = (o.immediateModes || []).indexOf((latest || status).mode) >= 0;
              toast(immediate
                ? "정지 신호를 보냈습니다 - 곧 반영됩니다."
                : "정지 신호를 보냈습니다. 실제로 멈추기까지 몇 초~수십 초 걸릴 수 있습니다(진행 중인 조회가 끝나야 함).");
              refresh();
              _pollUntilStopped(o.statusUrl, refresh);
            } catch (e) {
              toast(e.message, "error");
            }
          }, { title: `🔒 ${m.name} 자동매매 정지`, hint: "정지하려면 비밀번호를 다시 입력하세요." }),
        }));
        if (held > 0) {
          actions.appendChild(el("button", {
            class: "b danger", html: `${_dashIcon("pause")} 전량 청산 후 정지`,
            onclick: () => confirmPassword(async () => {
              if (!confirm(`보유 ${m.name} ${_heldCount(latest)}종목을 모두 정리하고 멈춥니다. 되돌릴 수 없습니다. 진행할까요?`)) return;
              try {
                const r = await api(o.stopUrl, { method: "POST", body: { close_positions: true } });
                toast(r && r.closed != null ? `${r.closed}종목 청산하고 정지했습니다.` : "전량 청산을 시작했습니다.");
                refresh();
                _pollUntilStopped(o.statusUrl, refresh);
              } catch (e) {
                toast(e.message, "error");
              }
            }, { title: `🔒 ${m.name} 전량 청산 후 정지`, hint: "진행하려면 비밀번호를 다시 입력하세요." }),
          }));
        }
      }
      head.appendChild(actions);
      out.appendChild(head);

      const summary = _controlSummary(m, status, running);
      if (summary) out.appendChild(el("div", { class: "hint ctl-sum", text: summary }));

      // 있을 때만 나오는 알림들
      if (!running && status.error) {
        out.appendChild(el("div", { class: "hint fall", text: "지난번 정지 사유: " + status.error }));
      }
      const halt = status.halt || {};
      if (running && halt.halted) {
        const box = el("div", { class: "banner warn", style: { marginTop: "var(--s2)" } });
        box.appendChild(el("div", { html: "<b>⏸ 신규 매수 중단 중</b>" }));
        if (halt.reason) box.appendChild(el("div", { class: "hint", text: halt.reason }));
        box.appendChild(el("div", { class: "hint", text: "보유 중인 종목의 청산(손절·익절)은 계속 진행됩니다." }));
        out.appendChild(box);
      }
      if (status.last_error && !halt.halted) {
        out.appendChild(el("div", { class: "hint fall", text: "마지막 오류: " + status.last_error }));
      }
      if (running && o.market === "overseas" && status.mode !== "sim" && status.market_open === false) {
        out.appendChild(el("div", { class: "hint", text: "지금은 미국 장이 닫혀 있어 보유 중인 종목만 관리합니다." }));
      }
      fill(body, out);
      } finally {
        _restoreScrollAfter(_savedTop, _scrollEl);
      }
    }

    refresh();
    clearInterval(_controlTimers[o.market]);
    _controlTimers[o.market] = setInterval(refresh, 5000);
    return card;
  }

  function renderEngineControlCard() {
    return _makeControlCard({
      market: "domestic", statusUrl: "/api/status", startUrl: "/api/engine/start", stopUrl: "/api/engine/stop",
      immediateModes: ["web", "sim", "replay"],
      offHelp: () => "시작하면 설정한 기법으로 종목을 고르고 매수·매도를 자동 실행합니다.",
    });
  }

  function renderCryptoControlCard() {
    return _makeControlCard({
      market: "crypto", statusUrl: "/api/crypto/status", startUrl: "/api/crypto/start", stopUrl: "/api/crypto/stop",
      immediateModes: ["web", "paper"],
      onStatus: (s) => { _lastCryptoStatus = s; updateBand(); },
      offHelp: (s) => {
        const techs = (s.entry_techniques || []).join(", ");
        return "시작하면 " + (techs ? `켜진 기법(${techs})으로` : "설정한 기법으로") + " 매수·매도를 자동 실행합니다.";
      },
    });
  }

  function renderOverseasControlCard() {
    return _makeControlCard({
      market: "overseas", statusUrl: "/api/overseas/status", startUrl: "/api/overseas/start", stopUrl: "/api/overseas/stop",
      immediateModes: ["web", "paper"],
      onStatus: (s) => { _lastOverseasStatus = s; _captureUsdKrwRate(s); updateBand(); },
      offHelp: () => "시작하면 국내주식과 같은 기법으로 매수·매도를 자동 실행합니다.\n\n"
        + "신규 진입은 미국 정규장 시간에만 하고, 그 밖의 시간에는 보유 중인 종목의 청산만 관리합니다.",
    });
  }

  function renderSwingControlCard() {
    return _makeControlCard({
      market: "swing", statusUrl: "/api/swing/status", startUrl: "/api/swing/start", stopUrl: "/api/swing/stop",
      immediateModes: ["web", "paper"],
      onStatus: (s) => { _lastSwingStatus = s; updateBand(); },
      offHelp: (s) => {
        const techs = (s.entry_techniques || []).join(", ");
        return "며칠~몇 주 보유하는 스윙 매매입니다. 국내·해외·암호화폐 세 시장을 함께 감시·보유하고"
          + (s.crypto_available === false ? "(암호화폐는 빗썸 키가 없어 지금은 건너뜁니다)" : "")
          + ", 일봉 기준으로 매일 정규장이 끝난 뒤 한 번만 종목을 다시 고릅니다(단타와 달리 최근 1주일 "
          + "실제 등락으로 선정). " + (techs ? `켜진 기법(${techs})으로` : "설정한 기법으로") + " 매수합니다.\n\n"
          + "보유 중인 종목의 손절·추적손절 관리는 장중에도 계속합니다.";
      },
    });
  }

  // 통합 탭의 상태 카드 - 시장별 자동매매 상태를 한 표로(조작 버튼은 각 시장 탭에서만).
  function renderMarketOverviewCard(entries) {
    const card = el("div", { class: "card ctl" });
    const head = el("div", { class: "ctl-head" });
    head.appendChild(el("b", { html: `${_dashIcon("layers")} 시장별 자동매매` }));
    head.appendChild(infoIcon("시작·정지 같은 조작은 각 시장 탭에서만 합니다 - 여러 시장을 한 번에 조작하는 건 위험할 수 있습니다."));
    card.appendChild(head);
    card.appendChild(_miniTable([
      { label: "시장", render: (r) => `${MARKETS[r.market].emoji} ${MARKETS[r.market].name}` },
      {
        label: "상태",
        render: (r) => el("span", {
          class: "ctl-badge " + (r.running ? "on" : "off"),
          text: r.running ? `${r.mode} 중` : `꺼짐 · ${r.mode}`,
        }),
      },
      { label: "보유", cls: "num", render: (r) => (r.held == null ? "-" : `${r.held}종`) },
      { label: "오늘 매매", cls: "num", render: (r) => (r.today == null ? "-" : `${r.today}건`) },
      {
        label: "오늘 실현손익", cls: "num",
        render: (r) => el("span", { class: dir(r.pnl), text: _signedMoneyOf(MARKETS[r.market].unit)(r.pnl) }),
      },
    ], entries));
    return card;
  }

  // ★★ panel.innerHTML="" 로 매번 전체를 지우고 다시 그리면 스냅샷이 올
  // 때마다(보통 1초 간격) 화면 전체가 깜빡인다. 게다가 로그 콘솔이 통째로
  // 재생성되면서 쌓아 둔 로그가 매번 사라지고, 암호화폐 카드도 재생성되며
  // 자체 폴링 타이머가 불안정해진다(실제로 겪은 문제). 패널 뼈대는 최초
  // 1회만 만들고, 이후엔 각 섹션 컨테이너의 내용만 교체한다.
  let _dashEls = null;
  let _dashMarketFilter = "all";  // ★ "통합"/"국내주식"/"해외주식"/"암호화폐" 필터 - 대시보드/종목선정 공통.

  // ★ 데코용 이모지는 아이콘으로 - window.UI.icon 이 아직 없을 수도 있어(다른 코스가
  //   추가하는 아이콘 세트) 항상 존재를 먼저 확인한다. 못 찾으면 조용히 빈 문자열.
  function _dashIcon(name) {
    return (window.UI && window.UI.icon) ? window.UI.icon(name) : "";
  }

  function _dashKpiCard(label, value, sub, cls) {
    const kpi = el("div", { class: "kpi" });
    kpi.appendChild(el("div", { class: "kpi-label", text: label }));
    kpi.appendChild(el("div", { class: "kpi-value " + (cls || ""), text: value }));
    kpi.appendChild(el("div", { class: "kpi-sub", text: sub || " " }));
    return kpi;
  }

  // ★★★ "스크롤을 많이 안 해도 첫 화면에서 가장 중요한 숫자가 보이게" - 시장마다(통합
  // 포함) 늘 같은 여섯 칸(모드·오늘 손익·보유·현금·오늘 거래·상태)을 상세 strip·탭보다
  // 앞에 둔다. status 는 시장별 상태 객체(또는 통합용으로 직접 구성한 값)를 그대로 받아
  // _tradeStats() 로 계산한다 - 아래 상세 strip(renderExternalStrip)과 같은 계산 기준이라
  // 숫자가 서로 어긋나 보이지 않는다.
  function _dashKpiGrid(status, opts) {
    opts = opts || {};
    const unit = opts.unit || "won";
    const s = _tradeStats(status);
    const money = _moneyOf(unit);
    const signedMoney = _signedMoneyOf(unit);
    const running = !!status.running;
    const modeTxt = opts.modeText || (running ? `${_modeText(status)} 중` : `꺼짐 · ${_modeText(status)}`);
    const halt = status.halt || (status.halted ? { halted: true, reason: status.halt_reason } : {});
    const haltTxt = halt.halted ? "매수 중단" : (status.degraded ? "시세 저하" : "정상");
    const grid = el("div", { class: "kpi-grid" });
    grid.appendChild(_dashKpiCard("모드", modeTxt));
    grid.appendChild(_dashKpiCard("오늘 손익", signedMoney(s.realized), "비용 " + money(Math.max(0, s.costs)), dir(s.realized)));
    grid.appendChild(_dashKpiCard("보유", `${s.heldCount}종`, money(s.heldAmount)));
    grid.appendChild(_dashKpiCard("현금", status.cash != null ? money(status.cash) : "-"));
    grid.appendChild(_dashKpiCard("오늘 거래", `${s.sellCount}건`, "승률 " + pct0(s.winRate)));
    grid.appendChild(_dashKpiCard("상태", haltTxt, halt.reason || " ", halt.halted ? "fall" : ""));
    return grid;
  }

  function renderMarketFilterSeg(currentValue, onChange) {
    const seg = el("div", { class: "seg" });
    [["all", "🔀 통합"], ["domestic", "🇰🇷 국내주식"], ["overseas", "🌍 해외주식"], ["crypto", "🪙 암호화폐"], ["swing", "📈 스윙"]].forEach(([key, label]) => {
      seg.appendChild(el("button", {
        text: label, class: key === currentValue ? "active" : "",
        onclick: (e) => {
          $$("button", seg).forEach((b) => b.classList.remove("active"));
          e.target.classList.add("active");
          onChange(key);
          // ★ 대시보드/종목선정/성과/매매일지 안의 시장 필터가 바뀌면(예: "국내주식"→
          // "해외주식") 통화 토글의 표시 여부도 같이 다시 계산한다 - onChange(key) 가
          // 먼저 모듈 전역 필터 변수(_dashMarketFilter 등)를 갱신해 둔 다음이라야
          // _currencyToggleRelevant() 가 새 값을 본다.
          if (typeof _updateCurrencyToggleVisibility === "function") _updateCurrencyToggleVisibility();
        },
      }));
    });
    return seg;
  }

  // 다섯 탭(통합·국내·해외·암호화폐·스윙)이 같은 다섯 칸을 같은 순서로 쓴다:
  // 상태 카드 → 지표 → 손익 추이 → 보유/감시 → 진행 로그.
  const DASH_MARKETS = ["all", "domestic", "overseas", "crypto", "swing"];

  function _buildDashSkeleton(panel) {
    const els = {
      marketFilter: el("div"),
      banners: el("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s3)" } }),
      m: {},
    };
    DASH_MARKETS.forEach((key) => {
      els.m[key] = {
        kpi: el("div"), card: el("div"), strip: el("div"), chart: el("div"), sym: el("div"),
        cols: el("div", { class: "cols grid-2" }), log: el("div"), tabsWrap: el("div"),
      };
    });
    const head = el("div", { class: "page-head" });
    head.appendChild(el("h1", { html: _dashIcon("dashboard") + " 대시보드" }));
    panel.appendChild(head);
    els.marketFilter.appendChild(renderMarketFilterSeg(_dashMarketFilter, (key) => {
      _dashMarketFilter = key;
      _applyDashMarketFilter();
    }));
    panel.appendChild(els.marketFilter);
    panel.appendChild(els.banners);
    // ★★★ "긴 스크롤 없이" - 시장별로 상태 카드·상세 지표 아래에 쌓이던
    // 손익추이·종목시세·보유목록·진행로그 네 덩어리를 탭(renderTabs)으로
    // 나눈다. 탭을 눌러도 실제 DOM 노드(g.chart/g.sym/g.cols/g.log)는 그대로
    // 재사용되므로(build() 가 매번 같은 참조를 돌려준다) 폴링·SSE 갱신은
    // 탭이 숨겨져 있어도 계속되고, 다시 그 탭을 열면 최신 내용이 바로 보인다.
    DASH_MARKETS.forEach((key) => {
      const g = els.m[key];
      panel.appendChild(g.kpi);
      panel.appendChild(g.card);
      panel.appendChild(g.strip);
      panel.appendChild(g.tabsWrap);
      renderTabs(g.tabsWrap, "dash:" + key, [
        { id: "holdings", label: "보유종목", build: () => g.cols },
        { id: "chart", label: "차트", build: () => g.chart },
        { id: "sym", label: "판단 근거", build: () => g.sym },
        { id: "log", label: "실시간 로그", build: () => g.log },
      ]);
    });
    // ★ 상태 카드와 국내 로그 콘솔은 여기서 딱 한 번만 채운다 - 이후 renderDashboard() 가
    //   몇 번을 다시 불려도 절대 건드리지 않는다(쌓인 로그·자체 폴링 타이머 유지).
    // ★ 국내도 해외·암호화폐와 같은 "최근 청산" 요약(엔진 내부 로그는 길고 어수선했다).
    _dashEls = els;
    refreshDomesticHistory(true);
    els.m.domestic.card.appendChild(renderEngineControlCard());
    els.m.crypto.card.appendChild(renderCryptoControlCard());
    els.m.overseas.card.appendChild(renderOverseasControlCard());
    els.m.swing.card.appendChild(renderSwingControlCard());
    ["domestic", "overseas", "crypto"].forEach((key) => {
      els.m[key].sym.appendChild(_buildSymbolChartCard(key).card);
    });
    _startExternalDashPolling(els);
    return els;
  }

  let _domHistKey = null;
  let _domHistAt = 0;

  // 국내 청산 이력(원장)을 해외·암호화폐와 같은 형태의 진행 로그로 보여준다. 청산이 새로 생기거나
  // 30초가 지나면 다시 불러온다.
  async function refreshDomesticHistory(force) {
    const n = ((_dashSnapshot && _dashSnapshot.closed) || []).length;
    const now = Date.now();
    if (!force && n === _domHistKey && now - _domHistAt < 30000) return;
    _domHistKey = n;
    _domHistAt = now;
    try {
      const d = await api("/api/performance?group=all");
      const rows = (d.trades || []).map((t) => ({ ...t, exit_time: t.exit_time ? Date.parse(t.exit_time) / 1000 : null }));
      if (_dashEls) fill(_dashEls.m.domestic.log, renderExternalLogConsole(rows, "name", "krw"));
    } catch (e) { /* 다음 주기에 */ }
  }

  let _externalDashPollTimer = null;
  let _refreshExternalDash = null;  // ★ SSE 스냅샷 도착 시 통합 화면을 즉시 갱신하기 위한 참조.

  const _sumPnl = (rows) => (rows || []).reduce((a, r) => a + (r.pnl || 0), 0);

  // 해외·암호화폐 탭과 통합 탭을 채운다(국내 탭은 SSE 스냅샷이 오는 renderDashboard 가 채운다).
  function _startExternalDashPolling(els) {
    async function refresh() {
      // ★ 5초 폴링(+ SSE 스냅샷 도착 시 즉시 호출)으로 보유/감시 카드
      // 등의 높이가 바뀔 수 있어 문서 스크롤을 보존한다.
      const _scrollEl = _scrollAnchorEl();
      const _savedTop = _scrollEl.scrollTop;
      try {
      // ★★★ 컨트롤 카드가 이미 5초마다 상태를 받아 _lastCryptoStatus/_lastOverseasStatus 에
      // 캐시해 둔다 - 여기서 같은 API 를 또 폴링하면 호출이 배로 늘고 화면끼리 다른 시점의
      // 값을 보여줄 수 있다. 캐시를 재사용하고, 한 번도 안 채워졌을 때만 직접 조회한다.
      let cryptoStatus = _lastCryptoStatus;
      if (!cryptoStatus) {
        try { cryptoStatus = await api("/api/crypto/status"); _lastCryptoStatus = cryptoStatus; } catch (e) { /* 꺼져 있다고 본다 */ }
      }
      cryptoStatus = cryptoStatus || { positions: {}, closed: [], watchlist: [] };
      let overseasStatus = _lastOverseasStatus;
      if (!overseasStatus) {
        try { overseasStatus = await api("/api/overseas/status"); _lastOverseasStatus = overseasStatus; } catch (e) { /* 꺼져 있다고 본다 */ }
      }
      overseasStatus = overseasStatus || { positions: {}, closed: [], watchlist: [] };
      _captureUsdKrwRate(overseasStatus);
      let swingStatus = _lastSwingStatus;
      if (!swingStatus) {
        try { swingStatus = await api("/api/swing/status"); _lastSwingStatus = swingStatus; } catch (e) { /* 꺼져 있다고 본다 */ }
      }
      swingStatus = swingStatus || { positions: {}, closed: [], candidates: [] };
      const snap = _dashSnapshot || {};

      // ★ 지표·손익 추이는 네 시장 모두 "오늘" 기준이다(국내 스냅샷의 closed 가 오늘 거래이므로).
      //   해외·암호화폐·스윙의 closed 는 최근 10건이라 서버가 보내는 closed_today 를 쓴다(스윙은
      //   거래 빈도가 낮아 closed_today 가 없으면 최근 10건을 그대로 쓴다).
      const cryptoToday = cryptoStatus.closed_today || cryptoStatus.closed || [];
      const overseasToday = overseasStatus.closed_today || overseasStatus.closed || [];
      const swingToday = swingStatus.closed_today || swingStatus.closed || [];
      const domesticToday = snap.closed || [];

      // ── 암호화폐·해외주식 탭: 국내와 같은 구성 ──
      [["crypto", cryptoStatus, cryptoToday, "won", "market"], ["overseas", overseasStatus, overseasToday, "usd", "symbol"]]
        .forEach(([key, status, today, unit, idKey]) => {
          const g = els.m[key];
          fill(g.kpi, _dashKpiGrid(Object.assign({}, status, { closed: today }), { unit }));
          fill(g.strip, renderExternalStrip({ positions: status.positions, closed: today }, key, unit));
          fill(g.chart, renderExternalPnlChart(today, key, status.positions));
          _symCharts[key].update(_chartSymbols(key, status, today));
          fillAll(g.cols, [
            renderHoldingsCard(_positionRows(key, status.positions)),
            renderWatchCard(_watchRowsFromWatchlist(key, status), { help: _watchHelp(key, status) }),
          ]);
          fill(g.log, renderExternalLogConsole(status.closed, idKey, unit));
        });

      // ── 스윙 탭: 감시 목록만 다르다(관심 종목이 아니라 추세 필터를 통과한 후보) ──
      {
        const g = els.m.swing;
        fill(g.kpi, _dashKpiGrid(Object.assign({}, swingStatus, { closed: swingToday }), { unit: "won" }));
        fill(g.strip, renderExternalStrip({ positions: swingStatus.positions, closed: swingToday }, "swing", "won"));
        fill(g.chart, renderExternalPnlChart(swingToday, "swing", swingStatus.positions));
        fillAll(g.cols, [
          renderHoldingsCard(_positionRows("swing", swingStatus.positions)),
          renderWatchCard(_watchRowsFromSwingCandidates(swingStatus), { help: _watchHelp("swing", swingStatus) }),
        ]);
        fill(g.log, renderExternalLogConsole(swingStatus.closed, "symbol", "won"));
      }

      // ── 통합 탭: 세 시장을 합친 같은 구성 ──
      const g = els.m.all;
      const dst = _lastDomesticStatus || {};
      const domHeld = Object.keys(snap.positions || {}).length;
      fill(g.card, renderMarketOverviewCard([
        {
          market: "domestic", running: !!dst.running, mode: _modeText(dst),
          held: dst.running ? domHeld : null, today: dst.running ? dst.trades : null, pnl: _sumPnl(domesticToday),
        },
        {
          market: "overseas", running: !!overseasStatus.running, mode: _modeText(overseasStatus),
          held: _heldCount(overseasStatus), today: overseasToday.length, pnl: _sumPnl(overseasToday),
        },
        {
          market: "crypto", running: !!cryptoStatus.running, mode: _modeText(cryptoStatus),
          held: _heldCount(cryptoStatus), today: cryptoToday.length, pnl: _sumPnl(cryptoToday),
        },
        {
          market: "swing", running: !!swingStatus.running, mode: _modeText(swingStatus),
          held: _heldCount(swingStatus), today: swingToday.length, pnl: _sumPnl(swingToday),
        },
      ]));

      // 통합 차트가 종목별로도 그릴 수 있게 종목 키(symbol)와 표시 이름(_label)을 붙인다.
      const allToday = [
        ...domesticToday.map((t) => ({
          pnl: t.pnl, exit_time: t.exit_time ? Date.parse(t.exit_time) / 1000 : null,
          symbol: t.symbol, _label: `🇰🇷 ${t.name || t.symbol}`,
        })),
        ...cryptoToday.map((r) => ({ ...r, _label: `🪙 ${r.market}` })),
        ...overseasToday.map((r) => ({ ...r, _label: `🌍 ${r.symbol}` })),
        ...swingToday.map((r) => ({ ...r, _label: `📈 ${r.name || r.symbol}` })),
      ];
      // ★★★ "종목선정 기법과 매매기법이 다르니 단타매매 대상으로 선정되도 스윙매매 대상이
      // 될 수 있다" - 같은 종목코드를 국내 단타와 스윙이 동시에 보유할 수 있으므로, 그냥
      // symbol 키로 합치면 한쪽이 없어져 보인다(보유 수·평가손익이 실제보다 적게 잡힘).
      // market:symbol 로 네임스페이스를 나눠 합친다 - 이 객체는 여기(통합 탭 요약)에서만 쓴다.
      const _nsPositions = (marketKey, positions) =>
        Object.fromEntries(Object.entries(positions || {}).map(([k, v]) => [`${marketKey}:${k}`, v]));
      const unifiedPositions = {
        ..._nsPositions("domestic", snap.positions), ..._nsPositions("crypto", cryptoStatus.positions),
        ..._nsPositions("overseas", overseasStatus.positions), ..._nsPositions("swing", swingStatus.positions),
      };

      // ★ 통합 탭의 KPI - 현금은 시장마다 통화가 달라(원화·달러) 단순 합산하지 않고 "-"로 둔다.
      const allRunning = [dst, overseasStatus, cryptoStatus, swingStatus].filter((st) => st && st.running).length;
      fill(g.kpi, _dashKpiGrid({
        running: allRunning > 0, positions: unifiedPositions, closed: allToday,
        halt: snap.halted ? { halted: true, reason: snap.halt_reason } : {},
        degraded: snap.degraded, cash: null,
      }, { unit: "won", modeText: `${allRunning}/4 시장 가동` }));

      const stripWrap = el("div");
      stripWrap.appendChild(renderExternalStrip({ positions: unifiedPositions, closed: allToday }, "unified", "won"));
      // ★★★ 통합 손익은 원화(국내·암호화폐)와 달러(해외)를 환산 없이 숫자로만 더한 값이다.
      if (overseasToday.length) {
        const note = el("div", { class: "hint" });
        note.appendChild(el("span", { text: "※ 원화·달러 단순 합산" }));
        note.appendChild(infoIcon(
          "통화가 다른 시장(원화·달러)을 환산 없이 숫자로만 합산했습니다.\n\n"
          + "정확한 금액이 아니라 대략적인 흐름 파악용입니다 - 정확한 값은 각 시장 탭에서 보세요."));
        stripWrap.appendChild(note);
      }
      fill(g.strip, stripWrap);
      fill(g.chart, renderExternalPnlChart(allToday, "unified", unifiedPositions));

      fillAll(g.cols, [renderHoldingsCard([
        ..._positionRows("domestic", snap.positions),
        ..._positionRows("overseas", overseasStatus.positions),
        ..._positionRows("crypto", cryptoStatus.positions),
        ..._positionRows("swing", swingStatus.positions),
      ], { showMarket: true, onRow: _goSelection })]);

      } finally {
        _restoreScrollAfter(_savedTop, _scrollEl);
      }
    }

    refresh();
    // ★★★ 통합 화면은 이 5초 폴링으로만 갱신되는데 국내 데이터는 SSE 로 수시로 도착한다 -
    // renderDashboard() 가 스냅샷을 받은 직후 이 함수를 직접 부를 수 있도록 밖으로 노출한다.
    _refreshExternalDash = refresh;
    clearInterval(_externalDashPollTimer);
    _externalDashPollTimer = setInterval(refresh, 5000);
  }

  // ★★ "통합/국내주식/해외주식/암호화폐"를 눌렀을 때 다른 시장의 칸은 숨긴다.
  // "통합"은 관찰 전용이다 - 시작·정지 같은 조작은 각 시장 탭에서 그 시장만 보면서 한다.
  function _applyDashMarketFilter() {
    if (!_dashEls) return;
    const f = _dashMarketFilter;
    DASH_MARKETS.forEach((key) => {
      const on = key === f;
      Object.values(_dashEls.m[key]).forEach((node) => { node.style.display = on ? "" : "none"; });
    });
  }

  function renderDashboard(snap) {
    _dashSnapshot = snap;
    const panel = $('.panel[data-panel="dash"]');
    if (!panel) return;
    // ★ SSE 스냅샷이 올 때마다(국내 매매 중에는 매우 잦다) 이 함수가 불린다 -
    // 보유/감시 카드 등의 높이가 바뀌면 문서 스크롤이 맨 위로 튈 수 있어 보존한다.
    const _scrollEl = _scrollAnchorEl();
    const _savedTop = _scrollEl.scrollTop;

    if (!_dashEls || !panel.contains(_dashEls.banners)) {
      // ★ 탭을 벗어났다 오면 innerHTML 이 비워질 수 있다 - 그럴 때만 새로 만든다.
      panel.innerHTML = "";
      _dashEls = _buildDashSkeleton(panel);
      _applyDashMarketFilter();
    }

    const banners = [];
    if (snap.halted) {
      banners.push(el("div", { class: "banner danger", text: "매매 중단: " + (snap.halt_reason || "") }));
    }
    if (snap.degraded) {
      banners.push(el("div", { class: "banner warn", text: "시세 연결이 저하되었습니다. 신규 진입을 멈춥니다." }));
    }
    if (snap.reconcile_alerts && snap.reconcile_alerts.length) {
      banners.push(el("div", { class: "banner warn", text: "계좌 대조 결과: " + snap.reconcile_alerts.join(" · ") }));
    }
    if (snap.size_reduced) {
      banners.push(el("div", { class: "banner warn", text: "연속 손절로 1회 매매 규모를 줄인 상태입니다 - 한 번 이익이 나면 원래대로 돌아옵니다." }));
    }
    const bannersWrap = el("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s3)" } });
    banners.forEach((b) => bannersWrap.appendChild(b));
    fill(_dashEls.banners, bannersWrap);

    const dm = _dashEls.m.domestic;
    // ★ 국내 KPI 는 SSE 스냅샷(snap - 보유·청산·중단 여부)과 /api/status 폴링
    //   (_lastDomesticStatus - 실행 여부·현금·모드)을 합쳐서 만든다. 각자 한쪽만 갖고 있다.
    fill(dm.kpi, _dashKpiGrid(Object.assign({}, _lastDomesticStatus || {}, {
      positions: snap.positions, closed: snap.closed,
      halt: snap.halted ? { halted: true, reason: snap.halt_reason } : {},
      degraded: snap.degraded,
    }), { unit: "won" }));
    fill(dm.strip, renderStrip(snap));
    fill(dm.chart, renderDashChart(snap));
    _symCharts.domestic.update(_chartSymbols("domestic", snap, snap.closed));
    fillAll(dm.cols, [
      renderHoldingsCard(_positionRows("domestic", snap.positions), { onRow: _goSelection }),
      renderWatchCard(_watchRowsFromCandidates(snap.candidates), {
        help: _watchHelp("domestic"), empty: _domesticEmptyReason(snap), onRow: _goSelection,
      }),
    ]);
    refreshDomesticHistory(false);

    // ★★★ 국내 스냅샷이 방금 갱신됐으니 통합 화면(실현손익·청산 건수·
    // 손익추이·전체 종목 시트)도 곧바로 다시 그린다 - 5초 폴링만 기다리면
    // 국내에서 방금 난 거래가 그동안 통합에서 빠져 보인다.
    if (_dashMarketFilter === "all" && typeof _refreshExternalDash === "function") {
      _refreshExternalDash();
    }
    _restoreScrollAfter(_savedTop, _scrollEl);
  }

  // ★ 엔진이 아직 안 돌고 있으면 SSE 스냅샷이 영원히 안 온다. 그렇다고
  // 화면을 빈 스켈레톤으로 영원히 둘 수는 없다 - 시작 버튼 자체가 스냅샷
  // 렌더링 안에 있어서, "스냅샷이 있어야 버튼이 보이는데 버튼을 눌러야
  // 스냅샷이 생기는" 닭과 달걀 문제가 된다. /api/setup 으로 최소한의
  // 정보만 받아 빈 스냅샷을 채워서라도 항상 뭔가는 보이게 한다.
  const EMPTY_SNAPSHOT = {
    mode: "web", degraded: false, halted: false, halt_reason: "", reconcile_alerts: [],
    positions: {}, closed: [], candidates: [], selection: null, techniques: [],
    equity_curve: [], pnl_curve: [], headroom: 0, weekly_pnl: 0, size_reduced: false,
    allocation: 0, realized_pnl: 0, trades: 0, daily_max_trades: 0, max_positions: 0,
    daily_loss_limit_pct: 0, round_trip_cost_pct: 0,
  };

  registerPanel("dash", {
    onShow: async () => {
      if (_dashSnapshot) {
        renderDashboard(_dashSnapshot);
        return;
      }
      const panel = $('.panel[data-panel="dash"]');
      if (panel) {
        panel.innerHTML = "";
        panel.appendChild(skeleton(80));
      }
      try {
        const setup = await api("/api/setup");
        if (_dashSnapshot) return; // 기다리는 사이 SSE로 진짜 스냅샷이 왔으면 그게 우선이다.
        renderDashboard(Object.assign({}, EMPTY_SNAPSHOT, { mode: setup.mode }));
      } catch (e) {
        if (panel) panel.appendChild(el("div", { class: "banner danger", text: "프로그램에 연결하지 못했습니다: " + e.message }));
      }
    },
  });

  // ★★ #band-title 은 index.html 에 "연결 중..." 이라는 초기값만 있고,
  // 이걸 실제로 갱신하는 코드가 어디에도 없었다 - 그래서 화면을 아무리
  // 써도 좌측 상단 문구가 영원히 "연결 중..."으로 고정되는 버그였다.
  // 원래 의도대로 지금의 매매 모드(관찰/시뮬레이션/모의매매/실거래/중단)를
  // 색과 문구로 보여주게 한다.
  // ★★ "시뮬레이션 모드로 매매 중"이라고만 하면 국내주식 얘기인지 암호화폐
  // 얘기인지 구분이 안 됐다(실제로 겪은 문제) - 시장별로 상태를 나눠 붙인다.
  let _lastStockSnap = null;
  let _lastCryptoStatus = null;
  let _lastOverseasStatus = null;
  let _lastSwingStatus = null;

  // ★ 국내주식 엔진이 꺼져 있으면 SSE 스냅샷이 안 와서(_lastStockSnap 없음) 이 밴드에서
  //   국내 항목이 통째로 사라졌다 - /api/status(꺼져 있어도 응답)로 항상 채운다.
  let _lastDomesticStatus = null;

  // ★ 좌측 상단(매매 상태)을 시장별 이모지 + 우하단 점(모드 색)의 아이콘으로 보여준다(글자 라벨
  // 없음) - 예전엔 "국내주식: 모의매매 중 · 해외주식: ..." 처럼 시장이 늘수록 문구가 계속 길어졌다.
  // 자세한 문구는 title(데스크톱 호버)과 클릭(모바일, #conn-indicator 와 같은 패턴)으로 본다.
  const STATE_LABEL_KO = {
    idle: "대기", sim: "시뮬레이션", paper: "모의매매", live: "실거래",
    halt: "중단", degraded: "저하", web: "관찰", replay: "리플레이",
  };

  function updateBand() {
    const band = $("#band");
    const title = $("#band-title");
    if (!band || !title) return;

    // 표시 순서: 국내주식 → 해외주식 → 암호화폐
    const modeNames = { web: "관찰", sim: "시뮬레이션", paper: "모의매매", live: "실거래" };
    let state = "idle";
    const icons = [];  // [{ emoji, dotState, text }]

    const ds = _lastDomesticStatus;
    const snap = _lastStockSnap;
    const domesticRunning = ds ? !!ds.running : !!snap;
    const mode = (snap && snap.mode) || (ds && ds.mode) || "";
    const modeLabel = MODE_LABELS[mode] || modeNames[mode] || mode;
    if (ds || snap) {
      let dState = "off";
      let text;
      if (!domesticRunning) {
        text = "국내주식: 꺼짐" + (modeLabel ? ` (${modeLabel})` : "");
      } else if (snap && snap.halted) {
        state = dState = "halt";
        text = "국내주식: 매매 중단됨" + (snap.halt_reason ? " · " + snap.halt_reason : "");
      } else if (snap && snap.degraded) {
        state = dState = "degraded";
        text = `국내주식: ${modeLabel} · 시세 연결 저하`;
      } else {
        state = dState = mode || "idle";
        // ★ 해외·암호화폐와 같은 표현("모의매매 중")으로 - 국내만 "모드"라고 다르게 쓰고 있었다.
        text = `국내주식: ${modeLabel} 중`;
      }
      icons.push({ emoji: "🇰🇷", dotState: dState, text });
    }

    if (_lastOverseasStatus) {
      const os = _lastOverseasStatus;
      const label = modeNames[os.mode] || (os.is_live ? "실거래" : "모의매매");
      const dState = os.running ? (os.mode || (os.is_live ? "live" : "paper")) : "off";
      const text = os.running ? `해외주식: ${label} 중` : `해외주식: 꺼짐 (${label})`;
      icons.push({ emoji: "🌍", dotState: dState, text });
    }

    if (_lastCryptoStatus) {
      const cs = _lastCryptoStatus;
      const label = modeNames[cs.mode] || (cs.is_live ? "실거래" : "모의매매");
      const dState = cs.running ? (cs.mode || (cs.is_live ? "live" : "paper")) : "off";
      const text = cs.running ? `암호화폐: ${label} 중` : `암호화폐: 꺼짐 (${label})`;
      icons.push({ emoji: "🪙", dotState: dState, text });
    }

    if (_lastSwingStatus) {
      const ss = _lastSwingStatus;
      const label = modeNames[ss.mode] || (ss.is_live ? "실거래" : "모의매매");
      const dState = ss.running ? (ss.mode || (ss.is_live ? "live" : "paper")) : "off";
      const text = ss.running ? `스윙: ${label} 중` : `스윙: 꺼짐 (${label})`;
      icons.push({ emoji: "📈", dotState: dState, text });
    }

    band.dataset.state = state;

    // ★ 사이드바 상단의 "현재 모드 배지"(#sidebar-mode-badge) 도 같은
    // state 를 그대로 반영한다 - 사이드바가 항상 떠 있는 데스크톱·태블릿
    // 에서는 상단바까지 안 봐도 왼쪽에서 바로 보인다.
    const sideBadge = $("#sidebar-mode-badge");
    if (sideBadge) {
      sideBadge.dataset.state = state;
      const label = STATE_LABEL_KO[state] || state;
      const labelEl = sideBadge.querySelector(".sidebar-mode-badge-label");
      if (labelEl) labelEl.textContent = label; else sideBadge.textContent = label;
    }

    title.innerHTML = "";
    if (!icons.length) {
      title.textContent = "연결 중...";
      return;
    }
    icons.forEach(({ emoji, dotState, text }) => {
      title.appendChild(el("span", {
        class: "band-icon", "data-state": dotState, title: text, text: emoji,
        onclick: () => toast(text),
      }));
    });
  }

  document.addEventListener("stream:snapshot", (ev) => {
    _lastStockSnap = ev.detail;
    updateBand();
    if (_activeTabIs("dash")) renderDashboard(ev.detail);
    else _dashSnapshot = ev.detail;
  });

  function _activeTabIs(id) {
    const a = $(`.sub-nav a[data-tab="${id}"]`);
    return !!(a && a.classList.contains("active"));
  }

  // ━━ 종목 선정 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  let _selectionData = null;
  let _selectionTimer = null;
  let _selectionGrids = {};

  async function loadSelection(refresh) {
    return api("/api/selection" + (refresh ? "?refresh=1" : ""));
  }

  function renderSourceBanner() {
    // 시세 출처 배너 - /api/source 를 한 번 불러 캐시한다.
    const box = el("div", { class: "banner" });
    api("/api/source")
      .then((s) => {
        let text = "시세 출처: " + (s.primary_label || s.source || "알 수 없음");
        if (s.limits && s.limits.length) text += " · " + s.limits[0];
        box.textContent = text;
        box.className = s.quality === "full" ? "banner" : "banner warn";

        // ★★★ 실제로 겪은 문제 - 라우터가 어떤 API 가 무슨 이유로 실패
        // 했는지 기록해 두는데도 화면 어디에도 안 나와서, "연계 테스트는
        // 통과하는데 시세를 못 가져온다"는 상황의 원인을 알 수 없었다.
        if (s.failures && Object.keys(s.failures).length) {
          box.className = "banner danger";
          box.appendChild(el("div", { html: `<b>${_selIcon("alert")} 최근 실패한 시세 API</b>` }));
          Object.entries(s.failures).forEach(([api_name, msg]) => {
            box.appendChild(el("div", { class: "hint", text: `· ${api_name}: ${msg}` }));
          });
        }
      })
      .catch(() => {
        box.textContent = "시세 출처를 확인하지 못했습니다.";
      });
    return box;
  }

  function renderRegimeBanner() {
    // 장 국면 배너는 STAGE 25b 에서 채운다. 지금은 자리만 확보해 둔다.
    return el("div", { class: "banner", id: "regime-banner", style: { display: "none" } });
  }

  function openModal(title, bodyNode) {
    // ★★★ "필요하면 팝업화면으로 분기해" - 긴 설명이나 표를 본문에 깔면
    // 화면이 스크롤로 길어진다. 모달로 띄우면 본문은 짧게 유지되고,
    // 필요한 사람만 열어 본다.
    const back = el("div", { class: "modal-back" });
    const box = el("div", { class: "modal-box" });
    const head = el("div", { class: "modal-head" });
    head.appendChild(el("b", { text: title }));
    const close = el("button", { class: "b ghost small", text: "✕", "aria-label": "닫기" });
    head.appendChild(close);
    box.appendChild(head);
    const body = el("div", { class: "modal-body" });
    body.appendChild(bodyNode);
    box.appendChild(body);
    back.appendChild(box);

    function shut() {
      back.remove();
      document.removeEventListener("keydown", onKey);
    }
    function onKey(e) { if (e.key === "Escape") shut(); }
    close.onclick = shut;
    // ★ 바깥(어두운 배경)을 눌러도 닫힌다 - 박스 안 클릭은 무시.
    back.onclick = (e) => { if (e.target === back) shut(); };
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
    return shut;
  }

  function infoIcon(text, opts) {
    // ★★★ "설명 텍스트를 아이콘이나 제목을 마우스 오버했을 때만 나타나도록"
    // - 긴 안내 문단이 화면을 가득 채워 정작 봐야 할 데이터가 묻혔다.
    // 설명은 아이콘 뒤에 숨기고, 마우스를 올리면 바로 보이게 한다
    // (클릭은 터치 기기·고정해서 읽고 싶을 때를 위해 함께 지원한다).
    opts = opts || {};
    const wrap = el("span", { class: "info-wrap" });
    const btn = el("button", {
      class: "info-dot", text: opts.label || "?", title: text,
      "aria-label": "설명 보기",
    });
    const pop = el("div", { class: "info-pop", text: text });
    pop.style.display = "none";

    // ★★★ 실제로 겪은 문제 - 표(.hscroll 가로 스크롤 컨테이너) 안에 있는 아이콘을 누르면
    // "설명이 안 보인다"는 신고가 들어왔다. pop 이 position:absolute 라서 가장 가까운
    // overflow 조상(.hscroll)에 잘려, display:block 이어도 화면 밖(또는 스크롤 밖)에
    // 그려졌기 때문이다. 화면 좌표 기준 position:fixed 로 띄우고 뷰포트를 벗어나지
    // 않게 매번 다시 계산한다 - 어떤 조상이 스크롤/overflow 를 걸어도 안 잘린다.
    function place() {
      const r = btn.getBoundingClientRect();
      pop.style.position = "fixed";
      const popWidth = pop.offsetWidth || 240;
      let left = r.left;
      if (left + popWidth > window.innerWidth - 8) left = Math.max(8, window.innerWidth - popWidth - 8);
      pop.style.left = left + "px";
      pop.style.top = (r.bottom + 4) + "px";
    }

    // ★ 마우스를 올리면 뜨고, 벗어나면 사라진다 - 클릭조차 필요 없다.
    let pinned = false;
    wrap.onmouseenter = () => { if (!pinned) { pop.style.display = "block"; place(); } };
    wrap.onmouseleave = () => { if (!pinned) pop.style.display = "none"; };
    btn.onclick = (e) => {
      e.stopPropagation();
      // ★ 클릭하면 고정된다 - 손을 떼도 남아 있어 읽기 편하다.
      $$(".info-pop").forEach((p) => { if (p !== pop) p.style.display = "none"; });
      pinned = !pinned;
      pop.style.display = pinned ? "block" : "none";
      if (pinned) place();
    };
    wrap.appendChild(btn);
    wrap.appendChild(pop);
    return wrap;
  }

  // ★ 바깥을 누르면 고정된 설명이 닫히게 - 한 번 열면 계속 떠 있으면 거슬린다.
  document.addEventListener("click", () => {
    $$(".info-pop").forEach((p) => { p.style.display = "none"; });
  });

  function titleWithHelp(titleText, helpText, tag) {
    // ★★★ "제목을 마우스 오버했을 때만 설명이 나타나도록" - 제목 자체에
    // 설명을 달아 두면, 본문에 안내 문단을 따로 둘 필요가 없다.
    const h = el(tag || "h2", { class: "has-help", title: helpText });
    h.appendChild(el("span", { text: titleText }));
    h.appendChild(infoIcon(helpText));
    return h;
  }

  function labelWithInfo(labelText, infoText) {
    // ★ 제목 옆에 작은 물음표를 붙인다 - 설명이 필요한 사람만 본다.
    const row = el("span");
    row.appendChild(el("span", { text: labelText }));
    row.appendChild(infoIcon(infoText));
    return row;
  }

  function renderCriteriaChips(criteria) {
    const wrap = el("div", { style: { display: "flex", flexWrap: "wrap", gap: "var(--s2)", margin: "var(--s2) 0" } });
    (criteria.text || []).forEach((t) => {
      wrap.appendChild(el("span", { class: "badge tech", style: { cursor: "default" }, text: t }));
    });
    return wrap;
  }

  // ━━ 종목 선정 목록(통합·국내·해외·암호화폐 공통) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // "전체 테마주 중 이 종목이 왜 선정(또는 탈락)됐는지"를 한 줄씩 - 선정 종목이 위, 탈락 종목은 사유와 함께 아래.
  // ★ 데코용 이모지는 아이콘으로.
  function _selIcon(name) {
    return (window.UI && window.UI.icon) ? window.UI.icon(name) : "";
  }
  // "선정만/전체" 2단이던 필터를 "선정 결과/탈락 사유/전체 종목" 3단으로 - 탈락 사유만 보고
  // 싶을 때도 전체를 스크롤해서 찾을 필요가 없게 한다.
  let _selShowMode = "selected"; // selected | rejected | all

  // 테마 하나와 그 구성 종목 하나 → 한 줄짜리 판정 근거.
  function _themeMemberWhy(t, m, unit) {
    if (m.selected) {
      const lead = m.rank_in_theme === 1 ? "대장주" : `거래대금 ${m.rank_in_theme}위`;
      return `테마 ${t.rank}위 · 동반상승 ${t.breadth}종목 · ${lead}`;
    }
    if (m.reject) return m.reject;
    if (!t.qualified) return "테마 미인정 - " + String(t.reason || "").split(" - ")[0];
    if (!t.picked) return `테마 ${t.rank}위(선정 테마 밖)`;
    if (!m.is_up) return m.reason || "동반 상승 아님";
    return "테마 후보 한도 초과";
  }

  // 테마 리포트(국내·미국) + 직접 추가한 관심 종목 → 행 목록. 같은 종목이 여러 테마에 있으면 선정된 쪽·높은 순위를 남긴다.
  function _selectionRows(report, opts) {
    opts = opts || {};
    const unit = opts.unit || "won";
    const best = new Map();
    (report && report.themes || []).forEach((t, ti) => {
      (t.members || []).forEach((m) => {
        const row = {
          symbol: m.symbol, name: m.name || m.symbol, theme: t.name, unit,
          selected: !!m.selected, last_price: m.last_price, change_rate: m.change_rate,
          trading_amount: m.trading_amount, why: _themeMemberWhy(t, m, unit),
          order: ti * 100 + (m.rank_in_theme || 99), inMarket: m.in_market,
        };
        const cur = best.get(m.symbol);
        if (!cur || (row.selected && !cur.selected) || (row.selected === cur.selected && row.order < cur.order)) {
          best.set(m.symbol, row);
        }
      });
    });
    // 관심 종목: 테마와 무관하게 항상 거래 대상 - 테마에서 탈락했더라도 선정으로 바꾼다.
    (opts.watch || []).forEach((w) => {
      const sym = typeof w === "string" ? w : w.symbol;
      const cur = best.get(sym);
      if (cur) {
        if (!cur.selected) { cur.selected = true; cur.why = "관심 종목 - 테마와 무관하게 거래 (테마 판정: " + cur.why + ")"; cur.theme = "관심 종목"; cur.order = 50000; }
        else { cur.why += " · 관심 종목이기도 함"; }
      } else {
        best.set(sym, {
          symbol: sym, name: (typeof w === "object" && w.name) || sym, theme: "관심 종목", unit, selected: true,
          last_price: null, change_rate: null, trading_amount: null, why: "직접 추가한 관심 종목 - 테마와 무관하게 거래", order: 50000,
        });
      }
    });
    const rows = [...best.values()];
    rows.sort((a, b) => (a.selected === b.selected ? a.order - b.order : a.selected ? -1 : 1));
    return rows;
  }

  function _selTable(rows, opts) {
    opts = opts || {};
    const usd = (v) => (v ? "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "-");
    const amt = (r) => (!r.trading_amount ? "-" : r.unit === "usd" ? "$" + Math.round(r.trading_amount / 1e6).toLocaleString("en-US") + "M" : eok(r.trading_amount));
    const cols = [];
    if (opts.showMarket) cols.push({ label: "", cls: "mk", render: (r) => MARKETS[r.assetMarket || r.market].emoji });
    cols.push({ label: "선정", render: (r) => el("span", { class: r.selected ? "rise" : "hint", text: r.selected ? "✓ 선정" : "탈락" }) });
    cols.push({
      label: "종목",
      render: (r) => el("span", { text: r.name !== r.symbol ? `${r.name} (${r.symbol})` : r.symbol }),
    });
    cols.push({ label: opts.groupLabel || "테마", render: (r) => r.theme || "-" });
    if (!opts.noPrice) {
      cols.push({
        label: "등락", cls: "num",
        render: (r) => (r.change_rate == null ? "-" : el("span", { class: dir(r.change_rate), text: pct(r.change_rate) })),
      });
      cols.push({ label: "거래대금", cls: "num", render: amt });
    }
    if (opts.status) cols.push({ label: "상태", render: (r) => r.state || "-" });
    cols.push({ label: "선정 기준·이유", cls: "why", render: (r) => r.why });
    const wrap = _miniTable(cols, rows);
    wrap.classList.add("sel-list");
    wrap.querySelectorAll("tbody tr").forEach((tr, i) => {
      const r = rows[i];
      if (r.symbol) tr.id = "selcand-" + r.symbol;  // 대시보드에서 종목을 눌러 이동해 올 때 강조하는 자리
      if (!r.selected) tr.classList.add("dim");
    });
    return wrap;
  }

  // 목록 카드: 제목 + 선정 기준 아이콘 + (선정만/전체) 전환 + 표.
  function _selectionCard(title, rows, opts) {
    opts = opts || {};
    const card = el("div", { class: "card" });
    const head = el("div", { class: "ctl-head" });
    const nSel = rows.filter((r) => r.selected).length;
    head.appendChild(el("b", { text: `${title} (선정 ${nSel}${rows.length !== nSel ? " / 전체 " + rows.length : ""})` }));
    if (opts.help) head.appendChild(infoIcon(opts.help));
    const holder = el("div");
    function draw() {
      const shown = _selShowMode === "all" ? rows
        : _selShowMode === "rejected" ? rows.filter((r) => !r.selected)
        : rows.filter((r) => r.selected);
      if (!shown.length) {
        const emptyText = _selShowMode === "rejected" ? "탈락한 종목이 없습니다." : (opts.empty || "선정된 종목이 없습니다.");
        fill(holder, el("div", { class: "hint empty", text: emptyText }));
        return;
      }
      fill(holder, _selTable(shown, opts));
    }
    if (rows.length !== nSel) {
      const seg = el("div", { class: "seg" });
      [["선정 결과", "selected"], ["탈락 사유", "rejected"], ["전체 종목", "all"]].forEach(([label, mode]) => {
        seg.appendChild(el("button", {
          text: label, class: _selShowMode === mode ? "active" : "",
          onclick: (e) => {
            _selShowMode = mode;
            $$("button", seg).forEach((b) => b.classList.remove("active"));
            e.target.classList.add("active");
            draw();
          },
        }));
      });
      const act = el("div", { class: "ctl-actions" });
      act.appendChild(seg);
      head.appendChild(act);
    }
    card.appendChild(head);
    if (opts.summary) card.appendChild(el("div", { class: "hint", text: opts.summary }));
    card.appendChild(holder);
    draw();
    return card;
  }

  function _criteriaHelp(report) {
    return ((report && report.criteria && report.criteria.text) || []).join("\n");
  }

  // 미국 종목 상태 문구(보유 여부).
  function _posState(status, symbol, unitFmt) {
    const p = (status.positions || {})[symbol];
    return p ? `보유 중 · 진입 ${unitFmt(p.entry_price)}` : "";
  }

  // 암호화폐: 전날 거래대금 상위 N종목(스테이블코인 제외)이 곧 감시 대상 - 테마는 없다.
  function _cryptoRows(status) {
    const list = status.watchlist || [];
    const held = status.positions || {};
    const n = status.top_volume_count || 10;
    const autoOn = !!status.auto_top_volume;
    return list.map((m, i) => {
      const pos = held[m];
      let why;
      let theme;
      if (autoOn && status.running && i < n) {
        theme = `상위 ${n}종목`;
        why = `전날 거래대금 ${i + 1}위(스테이블코인 제외)`;
      } else if (autoOn && status.running) {
        theme = "보유 중";
        why = "전날 거래대금 상위에선 빠졌지만 보유 중이라 청산 관리를 계속함";
      } else {
        theme = "기본 목록";
        why = autoOn ? "자동매매를 시작하면 전날 거래대금 상위 " + n + "종목으로 바뀝니다" : "설정에서 직접 정한 감시 목록";
      }
      return {
        symbol: m, name: m, theme, unit: "won", selected: true, last_price: null, change_rate: null, trading_amount: null,
        why, order: i, state: pos ? `보유 중 · 진입 ${won(pos.entry_price)}` : "",
      };
    });
  }

  // 스윙: 매일 정규장이 끝난 뒤 한 번 뽑은(추세 필터를 통과한) 후보.
  function _swingRows(status) {
    const held = status.positions || {};
    return (status.candidates || []).map((c, i) => {
      const pos = held[c.symbol];
      const assetMarket = (c.market && MARKETS[c.market]) ? c.market : "domestic";
      return {
        symbol: c.symbol, name: c.name || c.symbol, theme: c.theme || "", unit: MARKETS[assetMarket].unit,
        assetMarket, selected: true,
        last_price: null, change_rate: null, trading_amount: null,
        why: "테마 후보 + 스윙 관심 종목 중 중기 상승 추세(이동평균 위)·최근 1주 실제 상승 흐름을 모두 통과한 오늘의 후보",
        order: i, state: pos ? `보유 중 · 진입 ${_moneyOf(MARKETS[assetMarket].unit)(pos.entry_price)}` : "",
      };
    });
  }

  // 통합: 네 시장에서 선정된 종목만 한 표로.
  async function _renderSelectionAll(data) {
    const wrap = el("div");
    const rows = [];
    const dom = _selectionRows(data.selection, { watch: [] })
      .filter((r) => r.selected)
      .concat(((data.selection && data.selection.candidates) || [])
        .filter((c) => c.theme === "관심종목" && !(data.selection.themes || []).some((t) => (t.members || []).some((m) => m.symbol === c.symbol && m.selected)))
        .map((c) => ({ symbol: c.symbol, name: c.name, theme: "관심 종목", unit: "won", selected: true, last_price: c.last_price, change_rate: c.change_rate, trading_amount: c.trading_amount, why: "직접 추가한 관심 종목 - 테마와 무관하게 거래", order: 50000 })));
    const seenDom = new Set();
    dom.forEach((r) => { if (!seenDom.has(r.symbol)) { seenDom.add(r.symbol); rows.push({ ...r, market: "domestic" }); } });
    const [us, cs, ss, sws] = await Promise.all([
      api("/api/overseas/selection").catch(() => null),
      api("/api/crypto/status").catch(() => null),
      api("/api/overseas/status").catch(() => ({})),
      api("/api/swing/status").catch(() => null),
    ]);
    if (us) {
      _selectionRows(us.report, { unit: "usd", watch: us.watchlist || [] })
        .filter((r) => r.selected)
        .forEach((r) => rows.push({ ...r, market: "overseas", state: _posState(ss || {}, r.symbol, usd) }));
    }
    if (cs) _cryptoRows(cs).forEach((r) => rows.push({ ...r, market: "crypto" }));
    if (sws) _swingRows(sws).forEach((r) => rows.push({ ...r, market: "swing" }));
    if (!rows.length) {
      wrap.appendChild(el("div", { class: "hint", text: "지금 선정된 종목이 없습니다." }));
      return wrap;
    }
    const counts = ["domestic", "overseas", "crypto", "swing"].map((k) => `${MARKETS[k].emoji} ${rows.filter((r) => r.market === k).length}`).join(" · ");
    wrap.appendChild(_selectionCard("선정된 종목", rows, {
      showMarket: true, noPrice: true, status: true, groupLabel: "구분",
      summary: `${counts}  (선정 과정은 각 시장 탭에서 봅니다)`,
      help: "국내·해외는 테마 스크리닝으로 고른 종목과 직접 추가한 관심 종목, 암호화폐는 전날 거래대금 상위 종목입니다. 조건이 안 맞는 날은 사지 않는 것이 규칙입니다.",
    }));
    return wrap;
  }

  let _selectionMarketFilter = "all";

  async function renderSelectionOverseasSection() {
    const wrap = el("div");
    try {
      const [sel, status] = await Promise.all([api("/api/overseas/selection"), api("/api/overseas/status")]);
      const rep = sel.report;
      const rows = _selectionRows(rep, { unit: "usd", watch: sel.watchlist || [] });
      rows.forEach((r) => { r.state = _posState(status, r.symbol, usd); });
      const phase = rep && rep.phase_label ? rep.phase_label + " · " : "";
      const at = rep && rep.at ? String(rep.at).replace("T", " ").slice(11, 16) + " 기준 · " : "";
      wrap.appendChild(_selectionCard("미국 테마주 · 관심 종목", rows, {
        status: true,
        summary: sel.theme_select
          ? (rep ? `${phase}${at}${rep.summary || ""}` : (sel.idle || "아직 산정한 결과가 없습니다."))
          : (sel.idle || ""),
        help: (_criteriaHelp(rep) || "테마 점수 = 동반상승 종목수 × 상승률 중앙값 × log10(거래대금)")
          + "\n\n장이 바뀔 때마다(프리마켓·정규장·애프터마켓·야간) 다시 뽑습니다. 관심 종목은 테마와 별개로 항상 거래 대상입니다.",
        empty: "선정된 종목이 없습니다 - [설정] → 해외주식에서 관심 종목을 추가하거나 테마 자동 산정을 켜세요.",
      }));
      const errs = (rep && rep.errors) || [];
      if (errs.length) wrap.appendChild(el("div", { class: "banner warn", html: `${_selIcon("alert")} 일부 시세 조회 실패: ${esc(errs[0])}` }));
    } catch (e) {
      wrap.appendChild(el("div", { class: "hint", text: "불러오지 못했습니다: " + e.message }));
    }
    return wrap;
  }

  async function renderSelectionCryptoSection() {
    const wrap = el("div");
    try {
      const status = await api("/api/crypto/status");
      const rows = _cryptoRows(status);
      const techs = (status.entry_techniques || []).join(", ");
      wrap.appendChild(_selectionCard("암호화폐 감시 종목", rows, {
        noPrice: true, status: true, groupLabel: "구분",
        summary: techs ? `켜진 기법(${techs})을 모두 평가해 신호가 가장 강한 것으로 삽니다.` : "",
        help: "암호화폐는 테마로 고르지 않습니다 - 전날 거래대금 상위 10종목(스테이블코인 제외)에서 진입 신호가 뜬 것만 삽니다. 설정에서 끄거나 종목 수를 바꿀 수 있습니다.",
        empty: "감시 중인 코인이 없습니다. [설정] → 암호화폐에서 등록하세요.",
      }));
    } catch (e) {
      wrap.appendChild(el("div", { class: "hint", text: "상태를 불러오지 못했습니다: " + e.message }));
    }
    return wrap;
  }

  async function renderSelectionSwingSection() {
    const wrap = el("div");
    try {
      const status = await api("/api/swing/status");
      const rows = _swingRows(status);
      const techs = (status.entry_techniques || []).join(", ");
      wrap.appendChild(_selectionCard("스윙 후보 종목", rows, {
        noPrice: true, status: true, groupLabel: "구분",
        summary: techs ? `켜진 기법(${techs})을 모두 평가해 신호가 가장 강한 것으로 삽니다.` : "",
        help: "매일 정규장이 끝난 뒤 한 번, 국내 단타와 같은 테마 후보 풀 + 스윙 관심 종목 중 중기 상승 추세"
          + "(이동평균 위)인 종목만 남겨 다시 고릅니다. 진입 판정 자체는 일봉이 확정된 뒤에만 합니다.",
        empty: "지금 선정된 스윙 후보가 없습니다 - 아직 오늘 판정이 돌지 않았거나(정규장 종료 후 1회), 조건을 충족하는 종목이 없습니다.",
      }));
    } catch (e) {
      wrap.appendChild(el("div", { class: "hint", text: "상태를 불러오지 못했습니다: " + e.message }));
    }
    return wrap;
  }

  // ★★★ "장이 바뀔 때만 선정하는데 왜 화면이 깜빡이나" - 15초 폴링·스트림마다 패널을 통째로 지우고 다시 그려서
  // 깜빡였다. 시장 전환 버튼은 한 번만 만들고, 아래 내용은 버퍼에 그린 뒤 내용이 달라졌을 때만 바꿔 끼운다
  // (fill 이 같은 내용이면 건드리지 않는다). 빠르게 연달아 그릴 때는 마지막 것만 반영한다.
  let _selSeg = null;
  let _selHolder = null;
  let _selSeq = 0;

  function renderSelection(data) {
    _selectionData = data;
    const root = $('.panel[data-panel="selection"]');
    if (!root) return;
    if (!_selSeg || !root.contains(_selSeg)) {
      root.innerHTML = "";
      const head = el("div", { class: "page-head" });
      head.appendChild(el("h1", { html: _selIcon("target") + " 종목 선정" }));
      root.appendChild(head);
      _selSeg = renderMarketFilterSeg(_selectionMarketFilter, (key) => {
        _selectionMarketFilter = key;
        renderSelection(_selectionData);
      });
      _selHolder = el("div");
      root.appendChild(_selSeg);
      root.appendChild(_selHolder);
    }
    const seq = ++_selSeq;
    const buf = el("div");
    // ★ 후보 목록이 길 수 있어(스크롤해서 보는 화면) 15초마다 통째로 다시
    // 채워질 때 문서 스크롤이 맨 위로 튀지 않게 보존한다.
    const commit = () => {
      if (seq !== _selSeq) return;
      const _scrollEl = _scrollAnchorEl();
      const _savedTop = _scrollEl.scrollTop;
      fill(_selHolder, buf);
      _restoreScrollAfter(_savedTop, _scrollEl);
    };
    _buildSelection(data, buf, commit);
  }

  function _buildSelection(data, panel, commit) {
    const f = _selectionMarketFilter;
    if (f === "crypto") {
      renderSelectionCryptoSection().then((sec) => { panel.appendChild(sec); commit(); });
      return;
    }
    if (f === "overseas") {
      renderSelectionOverseasSection().then((sec) => { panel.appendChild(sec); commit(); });
      return;
    }
    if (f === "swing") {
      renderSelectionSwingSection().then((sec) => { panel.appendChild(sec); commit(); });
      return;
    }
    if (f === "all") {
      // ★★★ "통합에는 선정된 국내·미국·암호화폐 종목 리스트" - 선정 과정은 각 시장 탭에서 본다.
      _renderSelectionAll(data).then((sec) => { panel.appendChild(sec); commit(); });
      return;
    }
    // f === "domestic" - 국내주식 선정 과정(기존 내용 그대로)

    // ★★ 결론이 위, 근거가 아래.
    panel.appendChild(renderRegimeBanner());
    panel.appendChild(renderSourceBanner());

    const report = data.selection;

    // ★★★ "많은 스크롤 없이 첫 화면에서 가장 중요한 숫자가" - 신호가 아직 없어도
    // (report 가 없어도) 늘 같은 자리에 핵심 수치 세 칸을 보여준다.
    {
      const rs0 = data.rescreen || {};
      const nSelDom = report ? _selectionRows(report, { unit: "won", watch: [] }).filter((r) => r.selected).length : 0;
      const nAllDom = report ? ((report.candidates || []).length) : 0;
      const grid = el("div", { class: "kpi-grid" });
      const kpi = (label, value, sub) => {
        const k = el("div", { class: "kpi" });
        k.appendChild(el("div", { class: "kpi-label", text: label }));
        k.appendChild(el("div", { class: "kpi-value", text: value }));
        k.appendChild(el("div", { class: "kpi-sub", text: sub || " " }));
        return k;
      };
      grid.appendChild(kpi("선정 종목", report ? `${nSelDom}종` : "-", report ? `전체 ${nAllDom}종 중` : "아직 선정 전"));
      grid.appendChild(kpi("갱신 시각", report ? _hhmmss(report.at) : "-"));
      grid.appendChild(kpi("다음 재선정", rs0.next_at || "-", rs0.remain_minutes != null ? `${rs0.remain_minutes}분 뒤` : " "));
      panel.appendChild(grid);
    }

    if (!report) {
      // ★★★ "아직 스크리닝 결과가 없습니다"만 보여주면 고장으로 오해한다.
      // 실제로는 "신규 진입 시간(기본 09:20~14:00)이 지나서 스크리닝을
      // 하지 않는" 정상 동작인 경우가 많다 - 서버가 준 장 국면 정보로
      // 정확한 사유를 알려준다.
      const ses = data.session || {};
      const box = el("div", { class: ses.trading === false ? "banner" : "banner warn" });
      if (ses.trading === false && ses.why) {
        box.appendChild(el("div", { html: `<b>지금은 종목을 선정하지 않습니다 — ${esc(ses.label || "")}</b>` }));
        box.appendChild(el("div", { class: "hint", text: ses.why }));
        box.appendChild(el("div", {
          class: "hint",
          text: `이 프로그램은 국내 장 시간 전체가 아니라 설정된 신규 진입 구간(${ses.scan_start || "-"}~${ses.scan_end || "-"})에만 종목을 고르고 매수합니다`
            + " - 장 막판 변동성을 피하려는 의도된 설정입니다."
            + (ses.next_open ? ` 다음 진입 시작: ${ses.next_open}.` : "")
            + " 진입 구간을 바꾸려면 [설정] → 국내주식에서 조정하세요.",
        }));
      } else {
        // ★ 결론 한 줄 + 해결 방법은 아이콘으로.
        const nr = el("div");
        nr.appendChild(el("b", { text: "아직 스크리닝 결과가 없습니다." }));
        nr.appendChild(infoIcon(
          "화면을 열 때 스크리닝을 시도했는데도 결과가 없다면, 시세 조회 자체가 실패한 것입니다.\n\n"
          + "1) 바로 위 '시세 출처' 배너에 실패한 API가 표시되는지 확인하세요.\n"
          + "2) [준비·연결]의 연계 테스트에서 '거래대금 랭킹' 항목이 통과하는지 확인하세요."));
        box.appendChild(nr);
      }
      box.appendChild(el("button", {
        class: "b ghost small", text: "↻ 지금 다시 스크리닝",
        onclick: () => refreshSelection(true),
      }));
      panel.appendChild(box);
      commit();
      return;
    }

    // ★★★ "데이터·지표는 테이블로, 설명은 아이콘으로 대체해 화면을 깔끔하게"
    // 예전엔 선정 기준 칩 + 요약 + 갱신시각 + 재선정안내가 각각 한 줄씩
    // 차지해 화면 위쪽을 다 먹었다. 결론(요약) 한 줄만 크게 두고, 나머지는
    // 한 줄에 모아 아이콘 뒤로 숨긴다.
    panel.appendChild(el("div", { html: `<b>${esc(report.summary || "")}</b>`, style: { margin: "var(--s2) 0" } }));

    const metaRow = el("div", {
      class: "hint",
      style: { display: "flex", alignItems: "center", flexWrap: "wrap", gap: "var(--s3)", margin: "6px 0" },
    });
    metaRow.appendChild(el("span", { html: _selIcon("clock") + " " + _hhmmss(report.at) }));

    const rs = data.rescreen || {};
    if (rs.every_minutes) {
      let short = rs.next_at ? `↻ 다음 ${rs.next_at}` : "↻ 첫 선정 전";
      if (rs.next_at && rs.remain_minutes != null) {
        short += rs.remain_minutes > 0 ? ` (${rs.remain_minutes}분 뒤)` : " (곧)";
      }
      const cell = el("span");
      cell.appendChild(el("span", { text: short }));
      cell.appendChild(infoIcon(
        `종목 선정은 ${rs.every_minutes}분마다 다시 합니다. `
        + (rs.last_at ? `마지막 선정은 ${rs.last_at}이었고, ` : "")
        + (rs.next_at ? `다음은 ${rs.next_at}에 다시 고릅니다. ` : "아직 첫 선정 전입니다. ")
        + "주기를 바꾸려면 [설정] → 국내주식의 '재선정 주기'를 조정하세요."));
      metaRow.appendChild(cell);
    }

    // ★★★ "주기가 될 때까지 기다리지 않고 지금 바로 다시 선정하고
    // 싶다"는 요청 - 결과가 이미 있을 때는 새로고침 버튼이 아예 없어서,
    // 다음 자동 재선정 시각까지 기다리는 수밖에 없었다. 백엔드
    // (/api/selection?refresh=1 → rescreen(force=True))는 이미 있었으니
    // 화면에 버튼만 연결한다.
    const refreshBtn = el("button", {
      class: "b ghost small", text: "↻ 지금 갱신",
      onclick: async (e) => {
        e.target.disabled = true;
        e.target.textContent = "갱신 중…";
        try {
          await refreshSelection(true);
        } finally {
          // ★ refreshSelection() 이 성공하면 panel.innerHTML 이 통째로
          // 새로 그려져 이 버튼 자체가 사라진다 - 실패했을 때만 원상복구한다.
          if (document.body.contains(e.target)) {
            e.target.disabled = false;
            e.target.textContent = "↻ 지금 갱신";
          }
        }
      },
    });
    metaRow.appendChild(refreshBtn);

    // ★ 선정 기준은 평소엔 볼 일이 없다 - 아이콘 뒤에 넣는다.
    const crit = (report.criteria && report.criteria.text) || [];
    if (crit.length) {
      const cell = el("span");
      cell.appendChild(el("span", { html: _selIcon("sliders") + " 선정 기준" }));
      cell.appendChild(infoIcon(crit.join("\n")));
      metaRow.appendChild(cell);
    }
    panel.appendChild(metaRow);

    // ★★★ 실제로 겪은 문제 - 스크리너가 랭킹 조회에 실패하면 그 사유를
    // report.errors 에 정확히 담아 두는데, 화면이 이걸 전혀 안 보여줬다.
    // 그래서 "토스 API 는 연결됐는데 종목 선정이 안 된다"는 상황에서
    // 원인(어떤 API 가 무슨 이유로 실패했는지)을 알 방법이 없었다.
    if (report.errors && report.errors.length) {
      const errBox = el("div", { class: "banner danger" });
      errBox.appendChild(el("div", { html: `<b>${_selIcon("alert")} 시세 조회에 실패했습니다 - 아래가 실제 오류입니다</b>` }));
      report.errors.forEach((e) => {
        errBox.appendChild(el("div", { class: "hint", text: "· " + e }));
      });
      errBox.appendChild(el("div", {
        class: "hint",
        text: "이 오류가 해결되지 않으면 후보를 만들 수 없습니다. [준비·연결]의 연계 테스트에서 해당 API 항목을 확인하세요.",
      }));
      panel.appendChild(errBox);
    }

    // ★★★ "전체 테마주 중 이 종목이 선정된 기준과 이유를 리스트로 간결하게" - 선정 종목이 위, 탈락은 사유와 함께 아래.
    const watchCands = ((report && report.candidates) || []).filter((c) => c.theme === "관심종목").map((c) => ({ symbol: c.symbol, name: c.name }));
    panel.appendChild(_selectionCard("테마주 · 관심 종목", _selectionRows(report, { unit: "won", watch: watchCands }), {
      help: _criteriaHelp(report) || "테마 점수 = 동반상승 종목수 × 상승률 중앙값 × log10(거래대금)",
      empty: "선정된 종목이 없습니다.",
    }));
    commit();
  }

  async function refreshSelection(force) {
    try {
      const data = await loadSelection(force);
      renderSelection(data);
    } catch (e) {
      toast(e.message, "error");
    }
  }

  // ★ 아직 안 그려졌을 수 있으니 못 찾으면 0.4초 뒤 재시도(최대 8회).
  function focusCandidate(symbol, tries) {
    tries = tries || 0;
    const targetEl = document.getElementById("selcand-" + symbol);
    if (!targetEl) {
      if (tries >= 8) return;
      setTimeout(() => focusCandidate(symbol, tries + 1), 400);
      return;
    }
    targetEl.scrollIntoView({ behavior: "smooth", block: "center" });
    targetEl.classList.add("flip");
    setTimeout(() => targetEl.classList.remove("flip"), 1600);
  }

  registerPanel("selection", {
    onShow: () => {
      refreshSelection(false);
      if (_selectionTimer) clearInterval(_selectionTimer);
      // 15초마다 자동 갱신. document.hidden 이면 건너뛴다.
      _selectionTimer = setInterval(() => {
        if (document.hidden) return;
        if (!_activeTabIs("selection")) return;
        refreshSelection(false);
      }, 15000);
    },
  });

  document.addEventListener("stream:selection", (ev) => {
    if (_activeTabIs("selection")) renderSelection(ev.detail);
  });

  // ━━ 성과 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  // ★★★ 국내·해외·암호화폐·통합이 같은 요약 항목을 쓴다(누적 손익·거래·승률·손익비·MDD).
  // 예전엔 국내만 수익률까지 6칸, 나머지는 4칸이었다.
  // ★ 데코용 이모지는 아이콘으로.
  function _perfIcon(name) {
    return (window.UI && window.UI.icon) ? window.UI.icon(name) : "";
  }

  // ★★★ "스크롤 없이 첫 화면에서 가장 중요한 숫자가" - 예전 가로 strip 을
  // 공용 kpi-grid 카드(라벨/큰 값/보조줄)로 바꿨다. 계산식은 그대로다.
  function renderPerfSummary(t, currency) {
    // ★ el(..., {text}) 로 꽂히는 값(textContent)이라 텍스트("\n") 버전을 쓴다.
    const money = currency === "usd" ? _usdSignedWithKrwText : (v) => signed(v, "won");
    const plainMoney = currency === "usd" ? _usdWithKrwText : won;
    const grid = el("div", { class: "kpi-grid" });
    [
      // ★★★ "총 얼마가 투입되서 실현이익이 몇프로인지 알수있게" 요청 - "총 매수금액"(투입 원금)과
      // 그 대비 손익률을 추가한다(renderPerfBody 가 agg.trades 로 계산해 t.invested 에 실어 준다).
      { label: "총 매수금액", v: t.invested, fmt: plainMoney },
      { label: "누적 손익", v: t.pnl, fmt: money },
      { label: "수익률", v: t.return_pct, fmt: (v) => pct(v, 2) },
      { label: "거래", v: t.trades, fmt: (v) => String(Math.round(v || 0)) + "건" },
      { label: "승률", v: t.win_rate, fmt: (v) => pct0(v) },
      { label: "손익비", v: t.profit_factor, fmt: (v) => (v == null ? "∞" : v.toFixed(2)) },
      { label: "MDD", v: t.mdd, fmt: money },
    ].forEach((c) => {
      const cell = el("div", { class: "kpi" });
      cell.appendChild(el("div", { class: "kpi-label", text: c.label }));
      const tone = c.label === "누적 손익" || c.label === "MDD" || c.label === "수익률" ? dir(c.v || 0) : "";
      cell.appendChild(el("div", { class: "kpi-value " + tone, text: c.fmt(c.v) }));
      cell.appendChild(el("div", { class: "kpi-sub", text: " " }));
      grid.appendChild(cell);
    });
    return grid;
  }

  // 국내는 서버(ledger.totals)가 계산해 주고, 해외·암호화폐·통합은 청산 기록에서 직접 센다.
  function _perfTotals(trades) {
    const rows = (trades || []).slice().sort((a, b) => _timeValue(a.exit_time) - _timeValue(b.exit_time));
    let pnl = 0, wins = 0, grossWin = 0, grossLoss = 0, peak = 0, mdd = 0;
    rows.forEach((t) => {
      const p = t.pnl || 0;
      pnl += p;
      if (p > 0) { wins += 1; grossWin += p; } else if (p < 0) { grossLoss += -p; }
      peak = Math.max(peak, pnl);
      mdd = Math.min(mdd, pnl - peak);
    });
    return {
      pnl, trades: rows.length, win_rate: rows.length ? wins / rows.length : 0,
      profit_factor: grossLoss > 0 ? grossWin / grossLoss : (grossWin > 0 ? null : 0), mdd,
    };
  }

  function renderPerfGrid(container, data, view, currency) {
    // ★ "손익" 열은 bar:true + agg:"sum" 이라 같은 fmt 함수가 본문(innerHTML)과
    // 합계 행(textContent, DataGrid._aggregate 경로) 양쪽에서 쓰인다 - textContent 쪽에
    // HTML 태그가 그대로 글자로 보이면 안 되니 텍스트("\n") 버전을 쓴다.
    // "누적"/"MDD" 열은 tone:"pnl" 만 있고 agg 가 없어 본문(innerHTML)에만 쓰이므로
    // 작은 글씨 두 줄이 되는 HTML 버전을 쓸 수 있다.
    const fmtMoneyText = currency === "usd" ? _usdSignedWithKrwText : (v) => signed(v, "won");
    const fmtMoneyHtml = currency === "usd" ? _usdSignedWithKrw : (v) => signed(v, "won");
    container.innerHTML = "";
    if (view === "trades") {
      new DataGrid(container, {
        columns: [
          // ★★★ "성과에서 일자는 빼고 매도일시 기준 최신순" - 날짜 열은 매수·매도일시와 겹쳐서 뺐다.
          // ★★★ "날짜 외에 매매시간을 추가해달라"는 요청 - 같은 날 여러 번
          // 매매하면 날짜만으로는 순서·간격을 알 수 없었다. 거래 기록에
          // entry_time/exit_time(ISO 문자열)이 이미 있으니 시:분으로 보여준다.
          { key: "entry_time", label: "매수일시", fmt: (v) => _datetime(v) },
          { key: "exit_time", label: "매도일시", fmt: (v) => _datetime(v) },
          { key: "symbol", label: "코드" },
          { key: "name", label: "종목" },
          // ★★★ "기법이 매도 기법인 것 같다 - 매수 기법도 추가해달라"는
          // 지적. 맞다 - 기존 'technique' 는 청산 판정 결과라 매도 기법
          // 이었는데 라벨이 그냥 "기법"이라 오해를 부를 수밖에 없었다.
          // 매수·매도를 각각 명확히 표시한다.
          { key: "entry_technique", label: "매수 기법",
            fmt: (v) => v ? techBadgeHTML(v, { label: v }) : "-" },
          { key: "technique", label: "매도 기법",
            fmt: (v) => v ? techBadgeHTML(v, { label: v }) : "-" },
          // ★ 수량은 소수점 2자리까지만 - 암호화폐는 0.00123456 처럼
          //   길게 나와 표가 지저분했다. 정수는 소수점을 안 붙인다.
          { key: "qty", label: "수량", fmt: (v) => _qty(v), numeric: true },
          { key: "entry", label: "진입가", fmt: currency === "usd" ? _usdWithKrw : won, numeric: true },
          { key: "exit", label: "청산가", fmt: currency === "usd" ? _usdWithKrw : won, numeric: true },
          { key: "pnl", label: "손익", fmt: fmtMoneyText, bar: true, agg: "sum", numeric: true },
          { key: "reason", label: "사유" },
        ],
        // ★ 실제 매도 시각 기준 최신순(같은 시각이면 매수 시각 최신순)
        rows: (data.trades || []).slice().sort((a, b) =>
          _timeValue(b.exit_time) - _timeValue(a.exit_time) || _timeValue(b.entry_time) - _timeValue(a.entry_time)),
        expand: (row) => el("div", { text: row.why || row.reason || "선정·청산 사유가 남아있지 않습니다." }),
        storageKey: "grid.perf.trades",
      });
    } else if (view === "symbol") {
      // ★★★ "종목별로 총매수금액 총매도금액 수수료 실현이익을 알수있게" 요청.
      // 국내·해외·암호화폐·통합 네 탭 모두 이미 받아 둔 data.trades(개별 거래) 하나로
      // 계산한다(별도 서버 API 불필요) - "거래내역" 탭과 같은 원본을 쓴다.
      // ★ 수수료는 거래 기록에 따로 안 남아 있지만, pnl(실현이익)이 이미 수수료를
      // 뺀 값이므로 "세전 손익(청산가-진입가)×수량" 과의 차이가 곧 수수료(+세금 등
      // 그 시장이 떼는 모든 비용)다 - 시장마다 수수료율·세율·ETF 운용비 등 계산식이
      // 달라도 이 방식은 항상 정확하다.
      const bySymbol = {};
      (data.trades || []).forEach((t) => {
        const key = t.symbol || "-";
        const s = bySymbol[key] || (bySymbol[key] = {
          symbol: key, name: t.name || key, trades: 0,
          buyAmount: 0, sellAmount: 0, grossPnl: 0, pnl: 0, last_time: null,
        });
        const qty = Number(t.qty) || 0;
        const entry = Number(t.entry) || 0;
        const exit = Number(t.exit) || 0;
        s.trades += 1;
        s.buyAmount += entry * qty;
        s.sellAmount += exit * qty;
        s.grossPnl += (exit - entry) * qty;
        s.pnl += Number(t.pnl) || 0;
        if (_timeValue(t.exit_time) > _timeValue(s.last_time)) s.last_time = t.exit_time;
      });
      const rows = Object.values(bySymbol).map((s) => Object.assign(s, {
        fee: s.grossPnl - s.pnl,
        // ★ "실현이익이 몇프로인지" - 종목별로도 총매수금액 대비 수익률을 같이 보여준다.
        return_pct: s.buyAmount ? s.pnl / s.buyAmount : null,
      })).sort((a, b) => _timeValue(b.last_time) - _timeValue(a.last_time));
      new DataGrid(container, {
        columns: [
          { key: "symbol", label: "코드" },
          { key: "name", label: "종목" },
          { key: "trades", label: "거래", numeric: true },
          { key: "buyAmount", label: "총매수금액", fmt: currency === "usd" ? _usdWithKrw : won, numeric: true },
          { key: "sellAmount", label: "총매도금액", fmt: currency === "usd" ? _usdWithKrw : won, numeric: true },
          { key: "fee", label: "수수료(추정)", fmt: currency === "usd" ? _usdWithKrw : won, numeric: true },
          { key: "pnl", label: "실현이익", fmt: fmtMoneyText, bar: true, agg: "sum", numeric: true },
          { key: "return_pct", label: "수익률", fmt: (v) => pct(v, 2), numeric: true, tone: "pnl" },
          { key: "last_time", label: "마지막 매도일시", fmt: (v) => _datetime(v) },
        ],
        rows,
        storageKey: "grid.perf.symbol",
      });
    } else if (view === "daily") {
      new DataGrid(container, {
        columns: [
          { key: "date", label: "날짜" },
          { key: "last_exit_time", label: "마지막 매도일시", fmt: (v) => _datetime(v) },
          { key: "pnl", label: "손익", fmt: fmtMoneyText, bar: true, agg: "sum", numeric: true },
          { key: "trades", label: "거래", numeric: true },
          { key: "win_rate", label: "승률", fmt: (v) => pct0(v), numeric: true },
          { key: "cumulative", label: "누적", fmt: fmtMoneyHtml, numeric: true, tone: "pnl" },
        ],
        // ★ 서버·집계 모두 오래된 순으로 주므로(누적 계산 때문) 표시할 때만 뒤집는다.
        rows: (data.daily || []).slice().sort((a, b) => String(b.date).localeCompare(String(a.date))),
        storageKey: "grid.perf.daily",
      });
    } else if (view === "monthly") {
      new DataGrid(container, {
        columns: [
          { key: "month", label: "월" },
          { key: "pnl", label: "손익", fmt: fmtMoneyText, bar: true, agg: "sum", numeric: true },
          { key: "trades", label: "거래", numeric: true },
          { key: "win_rate", label: "승률", fmt: (v) => pct0(v), numeric: true },
          { key: "up_days", label: "상승일", numeric: true },
          { key: "cumulative", label: "누적", fmt: fmtMoneyHtml, numeric: true, tone: "pnl" },
        ],
        rows: (data.monthly || []).slice().sort((a, b) => String(b.month).localeCompare(String(a.month))),
        storageKey: "grid.perf.monthly",
      });
    } else {
      const rows = Object.entries(data.by_technique || {}).map(([k, v]) => Object.assign({ technique: k }, v))
        .sort((a, b) => _timeValue(b.last_time) - _timeValue(a.last_time));
      new DataGrid(container, {
        columns: [
          { key: "technique", label: "기법", fmt: (v) => techBadgeHTML(v, { label: v }) },
          { key: "last_time", label: "마지막 매도일시", fmt: (v) => _datetime(v) },
          { key: "trades", label: "거래", numeric: true },
          { key: "win_rate", label: "승률", fmt: (v) => pct0(v), numeric: true },
          { key: "profit_factor", label: "손익비", fmt: (v) => (v == null ? "∞" : v.toFixed(2)), numeric: true },
          { key: "pnl", label: "손익", fmt: fmtMoneyText, bar: true, agg: "sum", numeric: true },
          { key: "mdd", label: "MDD", fmt: fmtMoneyHtml, numeric: true, tone: "pnl" },
        ],
        rows,
        storageKey: "grid.perf.technique",
      });
    }
  }

  // ★★★ "거래내역/일자별/월별/기법별도 함께" - 암호화폐·해외주식의
  // raw 청산기록(closed[])을 국내주식 /api/performance 와 같은 4개
  // 구조(trades/daily/monthly/by_technique)로 이 화면에서 직접 집계한다.
  // 국내주식은 journal.py/ledger.py 가 서버에서 이미 계산해 주지만, 코인·
  // 해외주식은 완전히 별도 파일(crypto_state.json 등)이라 그런 집계
  // 파이프라인이 없다.
  function _aggregateFromTrades(trades) {
    const byDate = {};
    trades.forEach((t) => {
      const d = byDate[t.date] || (byDate[t.date] = { date: t.date, pnl: 0, trades: 0, wins: 0, last: null });
      d.pnl += t.pnl; d.trades += 1; if (t.pnl > 0) d.wins += 1;
      if (_timeValue(t.exit_time) > _timeValue(d.last)) d.last = t.exit_time;
    });
    let cum = 0;
    const daily = Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date)).map((d) => {
      cum += d.pnl;
      return {
        date: d.date, last_exit_time: d.last, pnl: d.pnl, trades: d.trades,
        win_rate: d.trades ? d.wins / d.trades : 0, cumulative: cum,
      };
    });

    const byMonth = {};
    trades.forEach((t) => {
      const month = (t.date || "").slice(0, 7);
      const m = byMonth[month] || (byMonth[month] = { month, pnl: 0, trades: 0, wins: 0 });
      m.pnl += t.pnl; m.trades += 1; if (t.pnl > 0) m.wins += 1;
    });
    let cumM = 0;
    const monthly = Object.values(byMonth).sort((a, b) => a.month.localeCompare(b.month)).map((m) => {
      cumM += m.pnl;
      return { month: m.month, pnl: m.pnl, trades: m.trades, win_rate: m.trades ? m.wins / m.trades : 0, up_days: m.wins, cumulative: cumM };
    });

    const byTech = {};
    trades.forEach((t) => {
      const k = t.technique || "-";
      const v = byTech[k] || (byTech[k] = { trades: 0, wins: 0, pnl: 0, grossWin: 0, grossLoss: 0, last: null });
      v.trades += 1; v.pnl += t.pnl;
      if (_timeValue(t.exit_time) > _timeValue(v.last)) v.last = t.exit_time;
      if (t.pnl > 0) { v.wins += 1; v.grossWin += t.pnl; } else { v.grossLoss += Math.abs(t.pnl); }
    });
    const by_technique = {};
    Object.entries(byTech).forEach(([k, v]) => {
      by_technique[k] = {
        trades: v.trades, last_time: v.last, win_rate: v.trades ? v.wins / v.trades : 0,
        profit_factor: v.grossLoss > 0 ? v.grossWin / v.grossLoss : Infinity,
        pnl: v.pnl, mdd: 0,
      };
    });

    return { trades, daily, monthly, by_technique };
  }

  // ★★★ "성과에서 매매기법을 클릭하면 설명이나와야" - 배지 클릭 팝업(forms.js)은
  // data-tech 가 /api/techniques 의 기법 key 와 정확히 같아야 매칭된다. 그런데 암호화폐·
  // 해외주식은 분할 매도로 끝난 거래의 reason 이 "momentum_fade (분할 매도 2회 후)"
  // 처럼 뒤에 설명이 덧붙어 있어(engine.py 의 국내주식도 같은 방식이지만, 국내는 이
  // 덧붙은 문자열을 "사유" 칸(reason)에만 쓰고 "매도 기법" 칸은 별도의 깨끗한 필드
  // (technique)를 쓴다) - key 가 안 맞아 클릭해도 아무 반응이 없었다. 뒤에 붙는
  // "(...)" 설명을 떼어 깨끗한 key 만 배지에 쓰고, 원문은 "사유" 칸에 그대로 둔다.
  function _cleanTechKey(reason) {
    return String(reason || "").replace(/\s*\([^()]*\)\s*$/, "").trim();
  }

  function _aggregateExternalPerf(closedRows, idKey) {
    const trades = (closedRows || []).map((r) => ({
      // ★ 로컬(한국) 날짜 - UTC 로 자르면 00~09시 거래가 전날로 집계돼 일자별 손익이 어긋났다.
      date: r.exit_time ? _splitDateTime(r.exit_time).date : "",
      // ★ 거래내역 표의 "매수 시각 / 매도 시각" 컬럼용 - 암호화폐·해외는
      //   유닉스 초로 저장되며, _hhmm() 이 두 형식을 모두 처리한다.
      entry_time: r.entry_time, exit_time: r.exit_time,
      symbol: r[idKey], name: r[idKey],
      // ★ 암호화폐·해외주식은 청산 사유(reason)가 곧 매도 기법이다.
      //   매수 기법은 별도 필드(entry_technique)에 저장해 뒀다.
      technique: _cleanTechKey(r.reason), entry_technique: r.entry_technique,
      qty: r.quantity, entry: r.entry_price, exit: r.exit_price, pnl: r.pnl || 0, reason: r.reason,
    }));
    return _aggregateFromTrades(trades);
  }

  async function loadPerf(group) {
    group = group || "virtual";
    try {
      renderPerf(await api("/api/performance?group=" + group), group);
    } catch (e) {
      toast(e.message, "error");
    }
  }

  let _perfMarketFilter = "all";  // 대시보드·매매일지와 같은 기본값("통합").

  // 요약 스트립 + 거래내역/일자별/월별/기법별 - 네 시장이 같은 구성을 쓴다.
  function renderPerfBody(panel, agg, totals, currency, note) {
    // ★ "총 얼마가 투입되서 실현이익이 몇프로인지" 요청 - 국내는 totals 를 서버가 주고
    // 크립토·해외·통합은 클라이언트가 계산해 오는 등 출처가 달라 totals 자체엔 투입금액이
    // 없다. agg.trades(모든 시장이 항상 가진 원본 거래 목록)로 여기서 한 번에 계산해 얹는다.
    const invested = (agg.trades || []).reduce(
      (sum, tr) => sum + (Number(tr.entry) || 0) * (Number(tr.qty) || 0), 0);
    const summaryTotals = Object.assign({}, totals, {
      invested, return_pct: invested ? totals.pnl / invested : null,
    });
    panel.appendChild(renderPerfSummary(summaryTotals, currency));
    if (note) {
      const n = el("div", { class: "hint" });
      n.appendChild(el("span", { text: note.text }));
      if (note.help) n.appendChild(infoIcon(note.help));
      panel.appendChild(n);
    }
    if (!totals.trades) {
      panel.appendChild(el("div", { class: "hint empty", text: "아직 매매 기록이 없습니다." }));
    }
    // ★★★ "많은 스크롤 없이" - 예전엔 직접 만든 세그먼트가 눌린 표 하나만 바꿔 끼웠는데(선택
    // 상태를 기억하지 않음), 공용 renderTabs() 로 바꿔 다른 메뉴에 갔다 와도 보던 표(거래내역/
    // 종목별/일자별/월별/기법별)가 그대로 유지된다.
    const gridTab = (id, currency2) => {
      const box = el("div");
      renderPerfGrid(box, agg, id, currency2);
      return box;
    };
    renderTabs(panel, "perf", [
      { id: "trades", label: "거래내역", build: () => gridTab("trades", currency) },
      { id: "symbol", label: "종목별", build: () => gridTab("symbol", currency) },
      { id: "daily", label: "일자별", build: () => gridTab("daily", currency) },
      { id: "monthly", label: "월별", build: () => gridTab("monthly", currency) },
      { id: "technique", label: "기법별", build: () => gridTab("technique", currency) },
    ]);
  }

  // ★★★ "모든 실적·이력 메뉴는 통합/국내주식/해외주식/암호화폐로 구분" + "네 탭의 내용과 항목을
  // 통일" - 필터 줄(시장 + 국내일 때만 연습/실거래) → 요약 → 하위 탭 순서가 모두 같다.
  async function renderPerf(domesticData, group) {
    const panel = $('.panel[data-panel="perf"]');
    panel.innerHTML = "";
    const head = el("div", { class: "page-head" });
    head.appendChild(el("h1", { html: _perfIcon("chart") + " 성과" }));
    panel.appendChild(head);
    const f = _perfMarketFilter;
    const filterRow = el("div", { class: "filter-row" });
    filterRow.appendChild(renderMarketFilterSeg(f, (key) => {
      _perfMarketFilter = key;
      renderPerf(domesticData, group);
    }));
    if (f === "domestic") {
      // ★★★ group=virtual(연습) 로만 고정 호출해서 실거래를 성과 화면에서 볼 수 없던 문제 -
      // 연습/실거래 원장은 백엔드가 이미 분리해 두었으니 전환 탭으로 고른다.
      const modeSeg = el("div", { class: "seg" });
      [["virtual", "🧪 연습"], ["real", "🔴 실거래"]].forEach(([id, label]) => {
        modeSeg.appendChild(el("button", { text: label, class: id === group ? "active" : "", onclick: () => loadPerf(id) }));
      });
      filterRow.appendChild(modeSeg);
      filterRow.appendChild(infoIcon(group === "real"
        ? "실제 계좌로 체결된 거래만 모았습니다."
        : "모의매매·시뮬레이션 거래만 모았습니다 - 실제 손익이 아닙니다."));
    }
    panel.appendChild(filterRow);

    if (f === "domestic") {
      renderPerfBody(panel, domesticData, domesticData.totals || _perfTotals(domesticData.trades), "won");
      return;
    }

    try {
      if (f === "crypto" || f === "overseas" || f === "swing") {
        const isKrx = f === "crypto" || f === "swing";
        const url = f === "crypto" ? "/api/crypto/journal" : f === "swing" ? "/api/swing/journal" : "/api/overseas/journal";
        const journalData = await api(url);
        const agg = _aggregateExternalPerf(journalData.rows || [], f === "crypto" ? "market" : "symbol");
        renderPerfBody(panel, agg, _perfTotals(agg.trades), isKrx ? "won" : "usd");
        return;
      }
      // f === "all"
      const [cryptoData, overseasData, swingData] = await Promise.all([
        api("/api/crypto/journal").catch(() => ({ rows: [] })),
        api("/api/overseas/journal").catch(() => ({ rows: [] })),
        api("/api/swing/journal").catch(() => ({ rows: [] })),
      ]);
      const cryptoTrades = _aggregateExternalPerf(cryptoData.rows || [], "market").trades
        .map((t) => ({ ...t, symbol: "🪙" + t.symbol, name: "🪙" + t.name }));
      const overseasTrades = _aggregateExternalPerf(overseasData.rows || [], "symbol").trades
        .map((t) => ({ ...t, symbol: "🌍" + t.symbol, name: "🌍" + t.name }));
      const swingTrades = _aggregateExternalPerf(swingData.rows || [], "symbol").trades
        .map((t) => ({ ...t, symbol: "📈" + t.symbol, name: "📈" + t.name }));
      // ★★★ "통합 성과는 요약만" - 거래내역·일자별 같은 세부 표는 각 시장 탭에서 보고, 여기서는
      // 네 시장을 합친 요약과 시장별 요약만 보여준다.
      const domTrades = domesticData.trades || [];
      const all = [...domTrades, ...cryptoTrades, ...overseasTrades, ...swingTrades];
      panel.appendChild(renderPerfSummary(_perfTotals(all), "won"));
      const note = el("div", { class: "hint" });
      note.appendChild(el("span", { text: "※ 원화·달러 단순 합산" }));
      note.appendChild(infoIcon("통화가 다른 시장(원화·달러)을 환산 없이 숫자로만 합산한 값입니다 - 대략적인 흐름 파악용이고, 정확한 값은 아래 시장별 표나 각 시장 탭에서 봅니다."));
      panel.appendChild(note);
      const perMarket = [["domestic", domesticData.totals || _perfTotals(domTrades)],
        ["overseas", _perfTotals(overseasTrades)], ["crypto", _perfTotals(cryptoTrades)],
        ["swing", _perfTotals(swingTrades)]].map(([m, t]) => ({ market: m, t }));
      const card = el("div", { class: "card" });
      card.appendChild(_miniTable([
        { label: "시장", render: (r) => `${MARKETS[r.market].emoji} ${MARKETS[r.market].name}` },
        { label: "누적 손익", cls: "num", render: (r) => el("span", { class: dir(r.t.pnl), text: _signedMoneyOf(MARKETS[r.market].unit)(r.t.pnl || 0) }) },
        { label: "거래", cls: "num", render: (r) => `${Math.round(r.t.trades || 0)}건` },
        { label: "승률", cls: "num", render: (r) => pct0(r.t.win_rate || 0) },
        { label: "손익비", cls: "num", render: (r) => (r.t.profit_factor == null ? "∞" : Number(r.t.profit_factor).toFixed(2)) },
        { label: "MDD", cls: "num", render: (r) => el("span", { class: dir(r.t.mdd || 0), text: _signedMoneyOf(MARKETS[r.market].unit)(r.t.mdd || 0) }) },
      ], perMarket));
      panel.appendChild(card);
    } catch (e) {
      panel.appendChild(el("div", { class: "hint", text: "불러오지 못했습니다: " + e.message }));
    }
  }

  registerPanel("perf", {
    onShow: () => loadPerf("virtual"),
  });

  // ━━ 매매일지 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function _truncate(s, max) {
    if (!s) return "";
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

  let _journalMarketFilter = "all";
  let _journalSessionFilter = "all";
  let _journalDate = null;

  // ★★★ "매매실적 조회시 장 세션별로도 조회할수 있게해" - 국내(프리장/본장/NXT장)와
  // 해외(프리장/본장/애프터장/데이장)만 세션 개념이 있다. 코인은 24시간 거래라 세션이
  // 없고, 스윙은 며칠~몇주 들고 가는 거래라 진입·청산 한 시점의 "세션" 하나로 묶기엔
  // 어색하지만, 그 안에 담긴 국내·해외 종목 레그는 청산 시각의 세션을 여전히 알 수
  // 있으니 두 세션 집합을 합쳐 보여준다(코인 레그는 session 자체가 없어 걸러진다).
  const SESSION_LABELS = { premarket: "프리장", regular: "본장", nxt: "NXT장", afterhours: "애프터장", overnight: "데이장" };
  const DOMESTIC_SESSIONS = ["premarket", "regular", "nxt"];
  const OVERSEAS_SESSIONS = ["premarket", "regular", "afterhours", "overnight"];

  function _journalSessionOptions(marketFilter) {
    // ★ "매매일지 통합과 스윙은 세션이 필요없음" - 코인처럼 세션 개념이 없거나(스윙 - 며칠~몇
    // 주 보유라 프리장/본장 구분이 의미 없음), 시장이 섞여 있어 세션 하나로 못 묶는 경우
    // (통합 - 국내·해외 세션 이름·시간대가 달라 뒤섞으면 오히려 헷갈림) 필터 자체를 숨긴다.
    if (marketFilter === "crypto" || marketFilter === "swing" || marketFilter === "all") return [];
    if (marketFilter === "overseas") return OVERSEAS_SESSIONS;
    return DOMESTIC_SESSIONS;  // "domestic"
  }

  function renderSessionFilterSeg(currentValue, options, onChange) {
    if (!options.length) return null;
    const seg = el("div", { class: "seg" });
    [["all", "전체 세션"], ...options.map((k) => [k, SESSION_LABELS[k] || k])].forEach(([key, label]) => {
      seg.appendChild(el("button", {
        text: label, class: key === currentValue ? "active" : "",
        onclick: (e) => {
          $$("button", seg).forEach((b) => b.classList.remove("active"));
          e.target.classList.add("active");
          onChange(key);
        },
      }));
    });
    return seg;
  }

  // ★★★ "국내·해외·암호화폐의 내용과 항목을 통일" - 국내는 일자별 매수/매도 이벤트 목록,
  // 해외·암호화폐는 청산 기록 목록이라 컬럼이 서로 달랐다. 셋 다 같은 모양의 이벤트
  // (시간·시장·모드·구분·종목·손익·내용)로 바꿔 같은 표에 담는다. 해외·암호화폐의 청산 기록에는
  // 진입 시각·가격이 들어 있으니 매수 이벤트도 함께 만들어, 국내와 같은 "매수→매도" 이력이 된다.
  function _modeBadgeOf(mode, isLive) {
    if (isLive || mode === "live") return "🔴 실거래";
    return `🧪 ${MODE_LABELS[mode] || (mode ? mode : "모의매매")}`;
  }

  function _domesticEvents(rows) {
    return (rows || []).map((r) => {
      const d = r.detail || {};
      return {
        ts: r.at ? Date.parse(r.at) / 1000 : 0, date: r.date || (r.at || "").slice(0, 10), time: r.time || "",
        market: "domestic", mode_badge: _modeBadgeOf(r.mode), session: r.session || null,
        kind: r.kind, label: r.symbol ? `${r.name || r.symbol} (${r.symbol})` : "",
        pnl: r.kind === "sell" && d.pnl != null ? d.pnl : null,
        text: _truncate(r.explain || "", 120), _raw: r,
      };
    });
  }

  function _externalEvents(marketKey, rows) {
    const unit = MARKETS[marketKey].unit;
    const money = _moneyOf(unit);
    const events = [];
    (rows || []).forEach((r) => {
      const id = marketKey === "crypto" ? r.market : r.symbol;
      const badge = _modeBadgeOf(null, r.is_live);
      if (r.entry_time) {
        const dt = _splitDateTime(r.entry_time);
        events.push({
          ts: r.entry_time, date: dt.date, time: dt.time, market: marketKey, mode_badge: badge,
          kind: "buy", label: id, pnl: null, session: r.session || null,
          text: `진입 ${money(r.entry_price)} × ${_qty(r.quantity)}${r.entry_technique ? " · " + r.entry_technique : ""}`,
          _raw: r,
        });
      }
      if (r.exit_time) {
        const dt = _splitDateTime(r.exit_time);
        events.push({
          ts: r.exit_time, date: dt.date, time: dt.time, market: marketKey, mode_badge: badge,
          kind: "sell", label: id, pnl: r.pnl != null ? r.pnl : null, session: r.session || null,
          text: `청산 ${money(r.exit_price)}${r.reason ? " · " + r.reason : ""}`,
          _raw: r,
        });
      }
    });
    return events;
  }

  // ★★★ "매매일지에 매수일시·매도일시를 넣고 매도일시 기준 최신순" - 매수와 매도를 따로 한 줄씩 늘어놓으면
  // 한 거래의 시작과 끝을 눈으로 이어 붙여야 했다. 거래 한 건을 한 줄로 묶어 두 시각을 함께 보여준다.
  // 아직 안 판 매수(보유 중)는 매도일시가 비어 맨 위에 온다.
  function _journalTradeRows(events) {
    const asc = events.slice().sort((a, b) => (a.ts || 0) - (b.ts || 0));
    const openBuys = new Map();  // 시장|종목 -> 아직 짝이 없는 매수 이벤트들
    const rows = [];
    const keyOf = (e) => e.market + "|" + e.label;
    asc.forEach((e) => {
      if (e.kind === "buy") {
        const k = keyOf(e);
        if (!openBuys.has(k)) openBuys.set(k, []);
        openBuys.get(k).push(e);
      } else if (e.kind === "sell") {
        const q = openBuys.get(keyOf(e)) || [];
        const buy = q.length ? q.pop() : null;
        const d = (e._raw && e._raw.detail) || {};
        const entryTs = (e._raw && e._raw.entry_time) || d.entry_time || (buy && buy.ts) || null;
        rows.push({
          ...e, entry_ts: entryTs, exit_ts: e.ts,
          text: (buy && buy.text ? buy.text + " → " : "") + e.text,
        });
      }
    });
    openBuys.forEach((q) => q.forEach((b) => {
      if (b._raw && b._raw.exit_time) return;  // 이미 팔린 거래(매도는 다른 날짜) - 보유 중이 아니다
      rows.push({ ...b, entry_ts: b.ts, exit_ts: null, pnl: null, text: b.text + " (보유 중)" });
    }));
    // 매도일시 최신순(보유 중인 것은 맨 위), 같으면 매수일시 최신순
    rows.sort((a, b) => {
      const ax = a.exit_ts == null ? Infinity : _timeValue(a.exit_ts);
      const bx = b.exit_ts == null ? Infinity : _timeValue(b.exit_ts);
      return (bx - ax) || (_timeValue(b.entry_ts) - _timeValue(a.entry_ts));
    });
    return rows;
  }

  function _journalGrid(events, showMarket, toolbarExtra) {
    const gridBox = el("div");
    const cols = [
      { key: "entry_ts", label: "매수일시", fmt: (v) => (v ? _datetime(v) : "-") },
      { key: "exit_ts", label: "매도일시", fmt: (v) => (v ? _datetime(v) : "보유 중") },
    ];
    if (showMarket) cols.push({ key: "market_label", label: "시장" });
    cols.push(
      { key: "mode_badge", label: "모드" },
      { key: "label", label: "종목" },
      // ★ 코인·스윙 코인 레그처럼 세션이 없는 행은 빈 칸으로 둔다 - 억지로 값을 채우지 않는다.
      { key: "session_label", label: "세션" },
      {
        key: "pnl", label: "손익", numeric: true, tone: "pnl",
        // ★ tone:"pnl" 만 있고 agg 는 없어 본문(innerHTML)에만 쓰인다 - 작은 글씨
        // 두 줄이 되는 HTML 버전(_signedMoneyOfHtml)을 쓸 수 있다.
        fmt: (v, row) => (v == null ? "" : _signedMoneyOfHtml(MARKETS[row.market].unit)(v)),
      },
      { key: "text", label: "내용" },
    );
    new DataGrid(gridBox, {
      columns: cols,
      rows: _journalTradeRows(events).map((e) => ({
        ...e,
        market_label: `${MARKETS[e.market].emoji} ${MARKETS[e.market].name}`,
        session_label: e.session ? (SESSION_LABELS[e.session] || e.session) : "",
      })),
      expand: (row) => (row._raw && row._raw.detail && row._raw.detail.terms) ? evidencePanel(row._raw.detail) : el("div", { text: "" }),
      page: 30, storageKey: "grid.journal.v4", toolbarExtra: toolbarExtra || null,
    });
    return gridBox;
  }

  // ★★★ "모든 실적·이력 메뉴는 통합/국내주식/해외주식/암호화폐로 구분해야 한다" - 매매일지도
  // 대시보드·성과와 같은 필터 구조. 날짜 선택도 네 탭이 같다(그날의 매수·매도만).
  async function loadJournal(date) {
    _journalDate = date || _journalDate;
    const panel = $('.panel[data-panel="journal"]');
    const f = _journalMarketFilter;
    panel.innerHTML = "";
    const head = el("div", { class: "page-head" });
    head.appendChild(el("h1", { html: (window.UI && window.UI.icon ? window.UI.icon("book") : "") + " 매매일지" }));
    panel.appendChild(head);
    const filterRow = el("div", { class: "filter-row" });
    filterRow.appendChild(renderMarketFilterSeg(f, (key) => {
      _journalMarketFilter = key;
      _journalSessionFilter = "all";  // ★ 시장이 바뀌면 그 시장에 없는 세션이 선택된 채로 남지 않게 초기화한다.
      loadJournal(_journalDate);
    }));
    // ★★★ "매매실적 조회시 장 세션별로도 조회할수 있게해" - 선택한 시장에 맞는 세션만 보여준다
    // (코인은 세션 자체가 없어 필터가 안 뜬다). 이미 받아 둔 행을 클라이언트에서 거를 뿐이라
    // 서버에 다시 묻지 않는다.
    const sessionOptions = _journalSessionOptions(f);
    if (!sessionOptions.includes(_journalSessionFilter)) _journalSessionFilter = "all";
    const sessionSeg = renderSessionFilterSeg(_journalSessionFilter, sessionOptions, (key) => {
      _journalSessionFilter = key;
      loadJournal(_journalDate);
    });
    if (sessionSeg) filterRow.appendChild(sessionSeg);
    panel.appendChild(filterRow);
    const body = el("div");
    panel.appendChild(body);
    body.appendChild(skeleton(80));

    const wantDomestic = f === "all" || f === "domestic";
    const wantCrypto = f === "all" || f === "crypto";
    const wantOverseas = f === "all" || f === "overseas";
    const wantSwing = f === "all" || f === "swing";

    let extEvents = [];
    try {
      if (wantCrypto) extEvents = extEvents.concat(_externalEvents("crypto", (await api("/api/crypto/journal")).rows));
    } catch (e) { /* 그 시장만 건너뛴다 */ }
    try {
      if (wantOverseas) extEvents = extEvents.concat(_externalEvents("overseas", (await api("/api/overseas/journal")).rows));
    } catch (e) { /* 그 시장만 건너뛴다 */ }
    try {
      if (wantSwing) extEvents = extEvents.concat(_externalEvents("swing", (await api("/api/swing/journal")).rows));
    } catch (e) { /* 그 시장만 건너뛴다 */ }

    let domData = null;
    if (wantDomestic) {
      try {
        domData = await api("/api/journal?group=virtual" + (_journalDate ? "&date=" + _journalDate : ""));
      } catch (e) { /* 그 시장만 건너뛴다 */ }
    }

    // 선택 가능한 날짜 = 세 시장 기록이 있는 날짜의 합집합. 기본은 가장 최근 날짜.
    const dates = new Set((domData && domData.dates) || []);
    extEvents.forEach((e) => { if (e.date) dates.add(e.date); });
    const sorted = Array.from(dates).sort();
    const selected = _journalDate && dates.has(_journalDate) ? _journalDate : (sorted[sorted.length - 1] || "");
    _journalDate = selected || _journalDate;

    // 국내 기록은 날짜별로 받아야 해서, 처음 받은 날짜와 선택한 날짜가 다르면 다시 받는다.
    if (wantDomestic && domData && selected && !_journalDate_matches(domData, selected)) {
      try {
        domData = await api("/api/journal?group=virtual&date=" + selected);
      } catch (e) { /* 그대로 둔다 */ }
    }

    let events = extEvents.filter((e) => e.date === selected);
    if (domData) events = events.concat(_domesticEvents(domData.rows).filter((e) => !selected || e.date === selected));
    if (_journalSessionFilter !== "all") events = events.filter((e) => e.session === _journalSessionFilter);
    events.sort((a, b) => (b.ts || 0) - (a.ts || 0));

    const dateSel = el("input", { type: "date", value: selected });
    dateSel.addEventListener("change", () => loadJournal(dateSel.value));
    body.innerHTML = "";
    if (!events.length) {
      const box = el("div", { class: "filter-row" });
      box.appendChild(dateSel);
      box.appendChild(el("span", { class: "hint", text: sorted.length ? "이 날짜에는 기록이 없습니다." : "아직 매매 기록이 없습니다." }));
      body.appendChild(box);
      return;
    }

    // ★★★ "스크롤 없이 첫 화면에서 가장 중요한 숫자가" - 표(또는 시장별 요약)를 보기 전에
    // 그날의 매수·매도·익절·손절 건수를 먼저 보여준다.
    {
      const sellsAll = events.filter((e) => e.kind === "sell");
      const buysAll = events.filter((e) => e.kind === "buy");
      const wins = sellsAll.filter((e) => (e.pnl || 0) > 0).length;
      const losses = sellsAll.filter((e) => (e.pnl || 0) < 0).length;
      const pnlSum = sellsAll.reduce((a, e) => a + (e.pnl || 0), 0);
      const kGrid = el("div", { class: "kpi-grid" });
      const kpi = (label, value, sub, cls) => {
        const k = el("div", { class: "kpi" });
        k.appendChild(el("div", { class: "kpi-label", text: label }));
        k.appendChild(el("div", { class: "kpi-value " + (cls || ""), text: value }));
        k.appendChild(el("div", { class: "kpi-sub", text: sub || " " }));
        return k;
      };
      kGrid.appendChild(kpi("매수", `${buysAll.length}건`));
      kGrid.appendChild(kpi("매도", `${sellsAll.length}건`));
      kGrid.appendChild(kpi("익절", `${wins}건`, "", wins ? "rise" : ""));
      kGrid.appendChild(kpi("손절", `${losses}건`, "", losses ? "fall" : ""));
      kGrid.appendChild(f === "all"
        ? kpi("실현손익", "-", "원화·달러 혼합 - 아래 표 참고")
        : kpi("실현손익", sellsAll.length ? _signedMoneyOf(f === "overseas" ? "usd" : "won")(pnlSum) : "-", "", dir(pnlSum)));
      body.appendChild(kGrid);
    }

    if (f === "all") {
      // ★★★ "통합은 요약만" - 세 시장의 매수·매도 이력을 한 표에 섞으면 너무 길다. 그날 시장별
      // 건수·승패·실현손익만 보여주고, 상세 이력은 각 시장 탭에서 본다.
      const rowsBy = ["domestic", "overseas", "crypto", "swing"].map((m) => {
        const ev = events.filter((e) => e.market === m);
        const sells = ev.filter((e) => e.kind === "sell");
        return {
          market: m, buys: ev.filter((e) => e.kind === "buy").length, sells: sells.length,
          wins: sells.filter((e) => (e.pnl || 0) > 0).length, losses: sells.filter((e) => (e.pnl || 0) < 0).length,
          pnl: sells.reduce((a, e) => a + (e.pnl || 0), 0),
        };
      });
      const bar = el("div", { class: "filter-row" });
      bar.appendChild(dateSel);
      bar.appendChild(infoIcon("통화가 다른 시장(원화·달러)이 섞여 있어 시장별로 나눠 보여줍니다. 상세 이력은 각 시장 탭에서 봅니다."));
      body.appendChild(bar);
      const card = el("div", { class: "card" });
      card.appendChild(_miniTable([
        { label: "시장", render: (r) => `${MARKETS[r.market].emoji} ${MARKETS[r.market].name}` },
        { label: "매수", cls: "num", render: (r) => `${r.buys}건` },
        { label: "매도", cls: "num", render: (r) => `${r.sells}건` },
        { label: "익절", cls: "num", render: (r) => el("span", { class: r.wins ? "rise" : "", text: `${r.wins}건` }) },
        { label: "손절", cls: "num", render: (r) => el("span", { class: r.losses ? "fall" : "", text: `${r.losses}건` }) },
        {
          label: "실현손익", cls: "num",
          render: (r) => el("span", { class: dir(r.pnl), text: r.sells ? _signedMoneyOf(MARKETS[r.market].unit)(r.pnl) : "-" }),
        },
      ], rowsBy));
      body.appendChild(card);
      return;
    }
    body.appendChild(_journalGrid(events, false, dateSel));
  }

  // 국내 응답의 행이 선택한 날짜 것인지(빈 응답이면 일치한다고 본다).
  function _journalDate_matches(domData, selected) {
    const rows = domData.rows || [];
    return !rows.length || rows.every((r) => (r.date || (r.at || "").slice(0, 10)) === selected);
  }

  registerPanel("journal", { onShow: () => loadJournal() });

  // ━━ 시장 평가(오늘의 시장 평가 - AI 요약) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★★★ "정리해서 보낸 내용을 프로그램에서 일자 시간별로 볼수 있게 추가해" 요청.
  // /api/review/market/history(market_commentary.save_review() 가 보낸 메시지를
  // 그대로 남긴 기록)를 날짜·시간순으로 보여준다. 텔레그램에 보낸 것과 같은 HTML(<b>·<i>·
  // <code>)이라 그대로 innerHTML 로 꽂아도 안전하다(전부 이 프로그램이 직접 만든 문자열이고,
  // 외부 입력이 그대로 섞여 들어가는 자리가 아니다 - compose() 참고).
  let _marketReviewFilter = "all";

  function _mrevIcon(name) {
    return (window.UI && window.UI.icon) ? window.UI.icon(name) : "";
  }

  async function loadMarketReviews() {
    const panel = $('.panel[data-panel="marketreview"]');
    panel.innerHTML = "";
    const head = el("div", { class: "page-head" });
    head.appendChild(el("h1", { html: _mrevIcon("globe") + " 시장 평가" }));
    panel.appendChild(head);
    const filterRow = el("div", { class: "filter-row" });
    const seg = el("div", { class: "seg" });
    [["all", "🔀 전체"], ["domestic", "🇰🇷 국내"], ["overseas", "🌍 해외"]].forEach(([key, label]) => {
      seg.appendChild(el("button", {
        text: label, class: key === _marketReviewFilter ? "active" : "",
        onclick: (e) => {
          $$("button", seg).forEach((b) => b.classList.remove("active"));
          e.target.classList.add("active");
          _marketReviewFilter = key;
          loadMarketReviews();
        },
      }));
    });
    filterRow.appendChild(seg);
    panel.appendChild(filterRow);
    const body = el("div");
    panel.appendChild(body);
    body.appendChild(skeleton(60));

    let rows = [];
    try {
      const q = _marketReviewFilter === "all" ? "" : "?market=" + _marketReviewFilter;
      rows = (await api("/api/review/market/history" + q)).rows || [];
    } catch (e) {
      body.innerHTML = "";
      body.appendChild(el("div", { class: "hint", text: e.message }));
      return;
    }
    body.innerHTML = "";
    if (!rows.length) {
      body.appendChild(el("div", {
        class: "hint",
        text: "아직 보낸 시장 평가가 없습니다. [설정] → 알림에서 '오늘의 시장 평가 보내기'를 켜면 본장·뉴욕장 마감 후 자동으로 쌓입니다.",
      }));
      return;
    }
    // ★★★ "스크롤 없이 첫 화면에서 가장 중요한 숫자가" - 목록을 보기 전에 건수 요약.
    const domCount = rows.filter((r) => r.market !== "overseas").length;
    const ovsCount = rows.filter((r) => r.market === "overseas").length;
    const kGrid = el("div", { class: "kpi-grid" });
    const kpi = (label, value, sub) => {
      const k = el("div", { class: "kpi" });
      k.appendChild(el("div", { class: "kpi-label", text: label }));
      k.appendChild(el("div", { class: "kpi-value", text: value }));
      k.appendChild(el("div", { class: "kpi-sub", text: sub || " " }));
      return k;
    };
    kGrid.appendChild(kpi("전체", `${rows.length}건`));
    kGrid.appendChild(kpi("국내", `${domCount}건`));
    kGrid.appendChild(kpi("해외", `${ovsCount}건`));
    kGrid.appendChild(kpi("최근 발송", _datetime(rows[0].sent_at)));
    body.appendChild(kGrid);

    // ★★★ "긴 스크롤 없이" - 최근 3건만 펼쳐서 보여주고, 그 이전 기록은
    // 접어 둔다(details.acc, 기본 접힘) - 지난 리뷰를 찾아볼 사람만 편다.
    const RECENT_N = 3;
    const buildCard = (r) => {
      const card = el("div", { class: "card" });
      const marketLabel = r.market === "overseas" ? "🌍 해외" : "🇰🇷 국내";
      card.appendChild(el("div", { class: "hint", text: `${marketLabel} · ${_datetime(r.sent_at)}` }));
      // ★ [2-7] r.text 는 뉴스 헤드라인을 바탕으로 LLM 이 만든 시장 평가 글 - 외부(뉴스 제목)에서
      // 온 문자열이 결국 이 안에 섞여 들어갈 수 있어 그대로 innerHTML 에 꽂으면 안 된다. esc() 로
      // 이스케이프하되, 줄바꿈은 문자 그대로 남아 있으니(컨테이너의 white-space: pre-line 이
      // 그 줄바꿈을 그대로 살려 보여준다) 개행이 없어지지 않는다.
      card.appendChild(el("div", { html: esc(r.text || ""), style: { marginTop: "var(--s2)", whiteSpace: "pre-line" } }));
      return card;
    };
    const list = el("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s3)" } });
    rows.slice(0, RECENT_N).forEach((r) => list.appendChild(buildCard(r)));
    const rest = rows.slice(RECENT_N);
    if (rest.length) {
      const acc = el("details", { class: "acc" });
      acc.appendChild(el("summary", { text: `이전 시장 평가 (${rest.length}건 더 보기)` }));
      const accBody = el("div", { class: "acc-body", style: { display: "flex", flexDirection: "column", gap: "var(--s3)" } });
      rest.forEach((r) => accBody.appendChild(buildCard(r)));
      acc.appendChild(accBody);
      list.appendChild(acc);
    }
    body.appendChild(list);
  }

  registerPanel("marketreview", { onShow: () => loadMarketReviews() });

  // ━━ 매매 기법 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  // ★★★ "심플&모던, 이모지 대신 아이콘" - 재편 대상 패널(매매 기법·소개·매매원칙·
  // 릴리즈노트·준비연결·테마·설정·월간 리뷰·실험실)에서 공통으로 쓰는 아이콘 헬퍼.
  // window.UI.icon 은 병렬로 작업 중인 다른 코스가 ui.js 에 추가하는 함수라 아직 이
  // 브랜치에는 없을 수 있다 - 없으면 조용히 빈 문자열을 돌려줘 레이아웃이 깨지지 않게 한다.
  function _pcIcon(name) {
    return (window.UI && typeof window.UI.icon === "function") ? window.UI.icon(name) : "";
  }
  // ★ 아이콘 + 텍스트를 함께 담은 노드를 만든다(el() 의 html/text 는 둘 중 하나만
  // 되므로 직접 조립한다). attrs 는 el() 에 그대로 넘어가므로 onclick·class 등을
  // 평소처럼 쓸 수 있다 - 버튼·h2·span 어디에나 같은 함수를 쓴다.
  function _pcIconEl(tag, iconName, text, attrs) {
    attrs = Object.assign({}, attrs);
    attrs.style = Object.assign({ display: "inline-flex", alignItems: "center", gap: "6px" }, attrs.style || {});
    delete attrs.text;
    delete attrs.html;
    const node = el(tag, attrs);
    const ic = _pcIcon(iconName);
    if (ic) node.insertAdjacentHTML("beforeend", `<span class="icon" aria-hidden="true">${ic}</span>`);
    node.appendChild(el("span", { text }));
    return node;
  }

  // ★★★ "사례별로 주가추이로 어떤 상황에서 종목을 선정하고 어떤 기법으로 매수·매도하는지
  // 그림으로" - 실제 시세가 아니라 각 기법의 발동 조건을 이해하기 쉽게 그린 도식이다.
  // 좌표는 daytrader/playbook.py 각 기법 클래스의 description/origin 을 그대로 옮긴 것으로,
  // 정확한 백테스트 결과가 아니라 "어떤 모양일 때 반응하는 기법인가"를 보여주는 예시다.
  const TECH_DIAGRAMS = {
    breakout: { pts: [[10,75],[60,65],[100,68],[140,60],[170,62],[200,45],[230,20],[270,12]],
      refY: 62, refLabel: "직전 고점", buyAt: 5,
      caption: "이미 오르던 종목이 직전 고점을 거래량과 함께 뚫는 순간 매수합니다." },
    theme_leader: { pts: [[10,80],[60,70],[110,55],[160,40],[210,25],[260,15]],
      pts2: [[10,82],[60,76],[110,68],[160,60],[210,55],[260,50]],
      buyAt: 2, caption: "같은 테마 종목들이 함께 오를 때(가는 선), 유동성이 가장 큰 대장주(굵은 선)를 매수합니다." },
    vwap_pullback: { pts: [[10,70],[50,50],[90,60],[120,72],[150,63],[190,40],[230,25],[270,15]],
      refY: 63, refLabel: "VWAP", buyAt: 4,
      caption: "강한 종목이 잠깐 쉬며 VWAP 근처까지 눌렸다가 다시 돌아설 때 매수합니다." },
    vwap_reclaim: { pts: [[10,50],[50,65],[90,75],[130,68],[160,55],[190,40],[230,28],[270,18]],
      refY: 55, refLabel: "VWAP", buyAt: 5,
      caption: "VWAP 아래로 밀렸던 종목이 거래량과 함께 VWAP 위로 다시 올라서는 순간 매수합니다." },
    open_gap: { pts: [[10,80],[40,45],[70,42],[100,48],[130,38],[160,30],[200,20],[240,15]],
      buyAt: 5, caption: "장 시작과 함께 위로 갭이 난 종목이 갭을 지키며 개장 첫 5분 고점을 거래량과 함께 뚫을 때 매수합니다(09:40 이후는 작동 안 함)." },
    close_squeeze: { pts: [[10,60],[60,45],[110,30],[160,22],[200,18],[240,14],[270,10]],
      buyAt: 5, caption: "장 막판(14:30~15:00)에도 당일 강세가 이어지고 거래량이 붙을 때 매수합니다(짧게 보유 후 15:10 강제청산)." },
    orb: { pts: [[10,50],[40,55],[70,48],[100,52],[130,50],[160,30],[200,18],[240,12]],
      refY: 50, refLabel: "개장 레인지 상단", buyAt: 5,
      caption: "장 시작 30분간 만들어진 레인지(박스)의 상단을 뚫는 흐름을 매수합니다." },
    volume_dry_pop: { pts: [[10,60],[60,62],[110,59],[160,61],[190,60],[220,25],[260,12]],
      buyAt: 5, caption: "거래가 말라붙어 횡보하던 종목에 갑자기 대량 거래가 붙는 첫 순간을 매수합니다." },
    bull_flag: { pts: [[10,80],[40,40],[70,20],[100,28],[130,35],[160,32],[190,15],[230,8]],
      refY: 32, refLabel: "깃발 상단", buyAt: 6,
      caption: "급등(깃대) 이후 짧게 눌린(깃발) 뒤 다시 깃발 상단을 돌파할 때 매수합니다." },
    ma_pullback: { pts: [[10,75],[50,55],[90,65],[120,58],[150,66],[190,45],[230,22],[270,12]],
      refY: 66, refLabel: "20EMA", buyAt: 5,
      caption: "추세가 확인된 종목이 20EMA 로 살짝 눌린 뒤 직전 봉 고가를 다시 돌파할 때 매수합니다." },
    volatility_breakout: { pts: [[10,70],[60,68],[100,72],[140,65],[160,45],[200,25],[240,15]],
      refY: 65, refLabel: "목표가(k×변동폭)", buyAt: 4,
      caption: "직전 구간 변동폭에 계수 k를 곱한 목표가를 현재가가 넘으면 매수합니다." },
    rsi_pullback: { pts: [[10,70],[50,50],[80,60],[100,78],[120,55],[160,35],[200,20],[240,12]],
      buyAt: 4, caption: "상승 추세 중 RSI(2)가 과매도 구간까지 짧게 떨어진 순간을 되돌림 매수 기회로 봅니다." },
    swing_ma_pullback: { pts: [[10,78],[60,55],[110,45],[150,60],[190,50],[230,25],[270,12]],
      refY: 60, refLabel: "20일선", buyAt: 4,
      caption: "60일선 위 상승 추세에서 20일선까지 눌렸다가 전날 고가를 다시 넘어서는 날 매수합니다(일봉 기준)." },
    swing_breakout: { pts: [[10,55],[50,58],[90,52],[130,56],[170,54],[210,30],[250,15]],
      refY: 54, refLabel: "20거래일 고가", buyAt: 5,
      caption: "최근 20거래일(약 한 달) 고가를 거래량을 동반해 뚫는 날 매수합니다(일봉 기준)." },
    swing_golden_cross: { pts: [[10,85],[70,70],[130,50],[190,35],[250,20]],
      pts2: [[10,60],[70,55],[130,48],[190,45],[250,42]],
      buyAt: 2, caption: "20일선(굵은 선)이 60일선(가는 선)을 최근 며칠 안에 상향 돌파(골든크로스)한 종목을 매수합니다." },
    fixed: { pts: [[10,50],[50,55],[90,45],[130,52],[170,35],[210,25],[250,15]],
      refY: 25, refLabel: "익절가", refY2: 75, refLabel2: "손절가", buyAt: 0, sellAt: 6,
      caption: "진입 즉시 손절가·익절가를 고정해 둡니다 - 둘 중 먼저 닿는 쪽에서 청산합니다(분할 매도를 쓰면 익절은 여기가 아니라 단계별로 나눠 처리됩니다)." },
    trailing: { pts: [[10,70],[50,55],[90,35],[130,20],[160,30],[190,45]],
      refY: 20, refLabel: "고점", buyAt: 0, sellAt: 5,
      caption: "일정 수익에 도달한 뒤 고점 대비 정해진 폭만큼 되돌리면, 남은 수익을 지키기 위해 청산합니다." },
    atr_stop: { pts: [[10,50],[50,55],[90,48],[130,60],[170,72],[210,80]],
      refY: 78, refLabel: "ATR 손절선", buyAt: 0, sellAt: 5,
      caption: "종목 고유의 변동성(ATR)만큼 물러난 자리를 손절선으로 씁니다 - 잔잔한 종목은 좁게, 출렁이는 종목은 넓게 잡힙니다." },
    time_stop: { pts: [[10,50],[50,52],[90,48],[130,53],[170,50],[210,52]],
      vLine: 210, vLabel: "보유시간 초과", buyAt: 0, sellAt: 5,
      caption: "정해진 시간을 넘기거나 오래 진전이 없으면, 가격과 무관하게 자리를 비워줍니다." },
    momentum_fade: { pts: [[10,60],[50,40],[90,25],[130,30],[160,35],[190,42]],
      refY: 38, refLabel: "VWAP", buyAt: 0, sellAt: 5,
      caption: "거래량 위축·연속 하락·VWAP 이탈 중 2개 이상이 겹치면, 목표에 못 미쳤어도 미리 정리합니다." },
    force_close: { pts: [[10,55],[60,45],[110,50],[160,40],[210,48],[250,42]],
      vLine: 250, vLabel: "15:10", buyAt: 0, sellAt: 5,
      caption: "동시호가 혼란을 피하기 위해 마감 전 정해진 시각에는 가격과 무관하게 전량 정리합니다(오버나이트를 허용해 두면 이 절차만 건너뜁니다)." },
  };

  function _techPathD(pts) {
    return pts.map((p, i) => (i === 0 ? "M" : "L") + p[0] + "," + p[1]).join(" ");
  }

  function _techMarkerSVG(pt, label, color) {
    return `<circle cx="${pt[0]}" cy="${pt[1]}" r="4" fill="${color}" stroke="var(--card)" stroke-width="1.5" />`
      + `<text x="${pt[0]}" y="${pt[1] < 20 ? pt[1] + 16 : pt[1] - 8}" font-size="9" fill="${color}" text-anchor="middle" font-weight="600">${label}</text>`;
  }

  function techDiagramSVG(key) {
    const d = TECH_DIAGRAMS[key];
    if (!d) return "";
    let extra = "";
    const refs = [["refY", "refLabel"], ["refY2", "refLabel2"]];
    refs.forEach(([yk, lk]) => {
      if (d[yk] != null) {
        extra += `<line x1="8" y1="${d[yk]}" x2="300" y2="${d[yk]}" stroke="var(--rule)" stroke-width="1" stroke-dasharray="4,3" />`;
        if (d[lk]) extra += `<text x="4" y="${d[yk] - 3}" font-size="8" fill="var(--muted)">${d[lk]}</text>`;
      }
    });
    if (d.vLine != null) {
      extra += `<line x1="${d.vLine}" y1="4" x2="${d.vLine}" y2="96" stroke="var(--rule)" stroke-width="1" stroke-dasharray="3,3" />`;
      if (d.vLabel) extra += `<text x="${d.vLine}" y="10" font-size="8" fill="var(--muted)" text-anchor="middle">${d.vLabel}</text>`;
    }
    const pts2 = d.pts2 ? `<path d="${_techPathD(d.pts2)}" fill="none" stroke="var(--faint)" stroke-width="1.5" />` : "";
    const path = `<path d="${_techPathD(d.pts)}" fill="none" stroke="var(--ink)" stroke-width="2" />`;
    let markers = "";
    if (d.buyAt != null) markers += _techMarkerSVG(d.pts[d.buyAt], "매수", "var(--series4)");
    if (d.sellAt != null) markers += _techMarkerSVG(d.pts[d.sellAt], "매도", "var(--series3)");
    return `<svg viewBox="0 0 300 100" style="width:100%;height:100px;display:block;overflow:visible;margin:var(--s2) 0">`
      + extra + pts2 + path + markers + `</svg>`;
  }

  function renderTechExamples(list) {
    const wrap = el("div");
    wrap.appendChild(el("div", {
      class: "hint",
      text: "각 기법이 실제로 어떤 주가 흐름에서 발동하는지 그린 예시 도식입니다 - 실제 시세 데이터가 아니라 "
        + "이해를 돕기 위한 그림입니다(점선은 그 기법이 보는 기준선, 점은 매수·매도가 일어나는 지점).",
    }));
    [["entry", "진입 기법 - 언제 사는가"], ["exit", "청산 기법 - 언제 파는가"]].forEach(([phase, heading]) => {
      // ★ /api/playbook 의 "all" 목록은 시장마다(국내·암호화폐 등) 같은 기법을 한 번씩 따로 담고
      // 있다(같은 기법을 시장별로 켜고 끌 수 있어서) - 여기서는 패턴 자체가 궁금한 것이라 기법당
      // 한 장만 보여주고, 대신 "어느 시장에서 켜져 있는지"를 한 줄로 모은다.
      const byKey = new Map();
      list.filter((t) => t.phase === phase && TECH_DIAGRAMS[t.key]).forEach((t) => {
        if (!byKey.has(t.key)) byKey.set(t.key, { ...t, markets: [] });
        byKey.get(t.key).markets.push({ market: t.market, enabled: t.enabled });
      });
      // ★ force_close(장 마감 강제청산)는 끌 수 없는 고정 규칙이라 /api/playbook 목록(켜고 끌 수
      // 있는 기법만 담음)에 아예 없다 - 그래도 실제 청산 사유에 늘 나오는 실존 기법이라 따로 더한다.
      if (phase === "exit" && !byKey.has("force_close")) {
        byKey.set("force_close", {
          key: "force_close", label: "장 마감 강제청산", phase: "exit",
          markets: [{ market: "domestic", enabled: true }, { market: "overseas", enabled: true }],
          alwaysOn: true,
        });
      }
      const items = Array.from(byKey.values());
      if (!items.length) return;
      wrap.appendChild(_pcIconEl("h2", phase === "entry" ? "trending-up" : "trending-down", heading));
      const cols = el("div", { class: "grid-2" });
      items.forEach((t) => {
        const card = el("div", { class: "card" });
        const onMarkets = t.markets.filter((m) => m.enabled).map((m) => (MARKETS[m.market] || {}).name || m.market);
        const statusText = t.alwaysOn ? "항상 켜짐(끌 수 없는 고정 규칙)"
          : onMarkets.length ? `켜짐: ${onMarkets.join("·")}` : "모든 시장에서 꺼짐";
        card.appendChild(el("div", {
          html: `<b>${esc(t.label)}</b> ${techBadgeHTML(t.key, { label: t.alwaysOn ? "고정" : onMarkets.length ? "켜짐" : "꺼짐" })}`,
        }));
        card.appendChild(el("div", { class: "hint", text: statusText }));
        card.appendChild(el("div", { html: techDiagramSVG(t.key) }));
        card.appendChild(el("div", { class: "hint", text: TECH_DIAGRAMS[t.key].caption }));
        cols.appendChild(card);
      });
      wrap.appendChild(cols);
    });
    return wrap;
  }

  function renderTechFull(list, phaseLabel) {
    // ★★★ "설명 화면도 같은 식으로 정리" - 예전엔 기법마다 카드가 하나씩
    // 깔리고 그 안에 설명·원전·표준값이 세 줄씩 들어가서, 9개 기법이면
    // 27줄이 넘어갔다. 정작 알고 싶은 것(어떤 기법이 켜져 있나)은 그
    // 사이에 묻혔다. 표 한 줄로 압축하고 긴 글은 아이콘 뒤로 숨긴다.
    // ★ "진입/청산 기법은 기본이 리스트를 볼 수 있도록" - 예전엔 접힌 <details> 라
    // 목록 자체(뭐가 켜져 있나)를 보려면 한 번 더 눌러야 했다. 기본을 펼쳐 두되,
    // 필요하면 여전히 접을 수 있게 <details> 는 그대로 둔다.
    const details = el("details", { open: true });
    details.appendChild(el("summary", { text: `${phaseLabel} 기법 ${list.length}종` }));

    const table = el("table", { class: "grid" });
    const thead = el("thead");
    const hr = el("tr");
    ["", "기법", "상태", "설명"].forEach((h) => hr.appendChild(el("th", { text: h })));
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = el("tbody");
    list.forEach((t) => {
      const tr = el("tr");
      tr.appendChild(el("td", { text: t.market === "crypto" ? "🪙" : "🇰🇷" }));
      tr.appendChild(el("td", { html: `<b>${esc(t.label)}</b> <span class="hint">${esc(t.key)}</span>` }));
      tr.appendChild(el("td", {
        class: t.enabled ? "rise" : "hint",
        text: t.enabled ? "켜짐" : "꺼짐",
      }));
      // ★ 설명·원전·표준값을 아이콘 하나에 모아 둔다 - 궁금할 때만 편다.
      const info = el("td");
      info.appendChild(infoIcon(
        `${t.description}\n\n[원전] ${t.origin}\n\n[표준값과 우리 설정] ${t.standard}`,
        { label: "ⓘ" }));
      tr.appendChild(info);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    details.appendChild(_scrollBox(table));
    return details;
  }

  function renderTechBonus(bonus) {
    const cards = el("div", { class: "grid-2" });
    const MARKETS = [
      { key: "domestic", label: "국내주식" },
      { key: "overseas", label: "해외주식" },
      { key: "crypto", label: "암호화폐" },
      { key: "swing", label: "스윙" },
    ];
    const MODE_LABEL = {
      none: "기본 룰만(가산점 없음)",
      entry_pref: "진입 가산점 반영(기본값)",
      entry_exit_pref: "진입+청산 가산점 반영",
    };
    MARKETS.forEach(({ key, label }) => {
      const m = (bonus && bonus[key]) || {};
      const card = el("div", { class: "card" });
      card.appendChild(el("div", { html: `<b>${label}</b>` }));
      const modeLabel = MODE_LABEL[m.learning_mode] || m.learning_mode || "-";
      card.appendChild(el("div", { html: `매매 모델: <b>${esc(modeLabel)}</b>` }));
      const list = m.bonus || [];
      if (!list.length) {
        card.appendChild(el("div", { class: "hint", text: "표시할 기법이 없습니다." }));
      } else {
        list.forEach((t) => {
          const row = el("div", { style: { margin: "var(--s2) 0" } });
          // ★ [2-7] t.multiplier 가 null 이면 .toFixed 호출이 그대로 죽는다(화면 전체가 안 그려짐) -
          // 숫자가 아니면 "-"로 보여준다. t.label 도 escape 해 서버 값이 마크업으로 해석되지 않게 한다.
          const multText = typeof t.multiplier === "number" ? `×${t.multiplier.toFixed(2)}` : "-";
          row.appendChild(el("div", { html: `${esc(t.label)} <b>${multText}</b>` }));
          row.appendChild(el("div", { class: "hint", text: t.why }));
          card.appendChild(row);
        });
      }
      cards.appendChild(card);
    });
    return cards;
  }

  function renderPlaybook(pb, hist, stats, bonus) {
    const panel = $('.panel[data-panel="playbook"]');
    panel.innerHTML = "";

    // ★★★ "설명 화면도 같은 식으로 정리" - 예전엔 안내 문단 2개 +
    // 자동선정 설명 카드 4개가 화면 위쪽을 통째로 차지해, 정작 봐야 할
    // 기법 목록이 한참 아래로 밀렸다. 한 줄로 줄이고 설명은 아이콘 뒤로.
    const guide = el("div", {
      class: "hint",
      style: { display: "flex", alignItems: "center", flexWrap: "wrap", gap: "var(--s3)", margin: "var(--s2) 0" },
    });

    const c1 = el("span");
    c1.appendChild(_pcIconEl("span", "book", "검증된 기법만 사용"));
    c1.appendChild(infoIcon(
      "여기 있는 기법은 모두 시장에서 널리 알려진, 문헌으로 검증된 것입니다. "
      + "각 기법의 원전과, 원전의 표준값을 우리가 어떻게 바꿨는지 목록의 ⓘ 에 적었습니다."));
    guide.appendChild(c1);

    const c2 = el("span");
    c2.appendChild(_pcIconEl("span", "lock", "켜짐/꺼짐은 수동"));
    c2.appendChild(infoIcon(
      "어떤 기법을 후보로 둘지는 사용자가 직접 정합니다 - 프로그램이 스스로 켜고 끄지 않습니다.\n\n"
      + "바꾸려면 [설정] → 종목·진입·청산 화면의 '진입 기법'/'청산 기법' 체크박스를 조정하세요.\n\n"
      + "[월간 리뷰]의 '제안'이 기법을 끄라고 권하는 경우가 있는데, 그것도 '반영'을 눌러야만 실제로 바뀝니다."));
    guide.appendChild(c2);

    const c3 = el("span");
    c3.appendChild(_pcIconEl("span", "sliders", "자동 선정 로직"));
    c3.appendChild(infoIcon(
      "켜진 기법이 여러 개일 때, 매 순간 시세를 평가해 '지금 가장 강한 신호'를 골라 매매합니다.\n\n"
      + "① 후보 종목을 전부 평가 — 오늘의 감시 종목마다 켜진 기법을 모두 돌려 신호 강도(0~1점)를 매깁니다.\n\n"
      + "② 기법은 '가장 강한 것'으로 — 여러 기법이 동시에 통과하면 설정 순서가 아니라 신호가 가장 강한 기법을 씁니다. 동점이면 설정한 우선순위를 따릅니다.\n\n"
      + "③ 검증된 기법에 가산점(실전 실적) — 과거 실제 매매 실적이 쌓인 기법은 승률에 따라 점수를 최대 ±30% 조정합니다. 단 거래 20건 미만이면 반영하지 않습니다(표본이 적으면 승률이 운에 좌우되어 과적합될 위험이 큽니다).\n\n"
      + "④ 검증된 기법에 가산점(최근 시세 백테스트) — [실험실]에서 종목·테마별로 최근 시세를 재생해 어떤 진입 기법이 가장 잘 맞았는지 미리 계산해 두면, 그 기법에 1.15배 가산점을 더 줍니다(technique_prefs). "
      + "★ 이 가산점은 진입(매수) 판정에만 적용됩니다 - 청산(매도)은 손절이 항상 먼저인 고정 우선순위(force_close→손절→ATR손절→익절→트레일링→모멘텀소멸→시간손절)라서, 여기에 백테스트 가산점을 넣으면 손절보다 다른 청산이 먼저 발동할 위험이 있어 적용하지 않습니다.\n\n"
      + "⑤ 강한 신호부터 매수 — 최종 점수가 높은 순서로 삽니다. 하루 거래 한도에 걸려 못 산 종목은 그 사유가 표시됩니다.\n\n"
      + "실제 선택 근거(③④ 가산점 포함)는 국내주식의 경우 [매매일지]의 각 매수 기록에 남습니다(해외주식·암호화폐·스윙은 아직 일지에 이 근거 문구가 남지 않습니다). "
      + "끄려면 config.yaml 의 strategy.best_signal 을 false 로 두세요."));
    guide.appendChild(c3);

    panel.appendChild(guide);

    const allList = pb.all || [];
    const enabled = pb.enabled || [];
    renderTabs(panel, "playbook", [
      { id: "entry", label: "진입 기법", build: () => renderTechFull(allList.filter((t) => t.phase === "entry"), "진입") },
      { id: "exit", label: "청산 기법", build: () => renderTechFull(allList.filter((t) => t.phase === "exit"), "청산") },
      { id: "example", label: "사례로 보기", build: () => renderTechExamples(allList) },
      { id: "bonus", label: "시장별 모델·가산점", build: () => renderTechBonus(bonus) },
      {
        id: "on", label: `켜진 기법 (${enabled.length})`,
        build: () => {
          const cards = el("div", { class: "grid-2" });
          enabled.forEach((t) => {
            const card = el("div", { class: "card" });
            card.appendChild(el("div", { html: `<b>${esc(t.label)}</b> ${techBadgeHTML(t.key, { label: t.phase === "entry" ? "진입" : "청산" })}` }));
            card.appendChild(el("div", { class: "hint", text: t.description }));
            cards.appendChild(card);
          });
          return cards;
        },
      },
      {
        id: "perf", label: "오늘·실적",
        build: () => {
          const box = el("div");
          box.appendChild(el("h2", { text: "오늘 어디서 막혔나" }));
          const histBox = el("div");
          const entries = Object.entries(hist.histogram || {}).sort((a, b) => b[1] - a[1]);
          if (!entries.length) {
            histBox.appendChild(el("div", { class: "hint", text: "오늘은 막힌 기록이 없습니다." }));
          } else {
            const maxV = Math.max.apply(null, entries.map((e) => e[1]));
            entries.forEach(([key, count]) => {
              const row = el("div", { class: "histogram-row" });
              row.appendChild(el("span", { text: key, style: { minWidth: "140px" } }));
              const bar = el("div", { class: "histogram-bar" });
              bar.appendChild(el("i", { style: { width: (count / maxV) * 100 + "%" } }));
              row.appendChild(bar);
              row.appendChild(el("span", { text: String(count) }));
              histBox.appendChild(row);
            });
          }
          box.appendChild(histBox);
          box.appendChild(el("h2", { text: "기법별 성적" }));
          const gridBox = el("div");
          const rows = Object.entries(stats).map(([k, v]) => Object.assign({ technique: k }, v));
          new DataGrid(gridBox, {
            columns: [
              { key: "technique", label: "기법", fmt: (v) => techBadgeHTML(v, { label: v }) },
              { key: "trades", label: "거래", numeric: true },
              { key: "win_rate", label: "승률", fmt: (v) => pct0(v), numeric: true },
              { key: "profit_factor", label: "손익비", fmt: (v) => (v == null ? "∞" : v.toFixed(2)), numeric: true },
              { key: "pnl", label: "손익", fmt: (v) => signed(v, "won"), bar: true, agg: "sum", numeric: true },
            ],
            rows,
            storageKey: "grid.playbook.stats",
          });
          box.appendChild(gridBox);
          return box;
        },
      },
    ]);
  }

  registerPanel("playbook", {
    onShow: async () => {
      try {
        const [pb, hist, stats, bonus] = await Promise.all([
          api("/api/playbook"),
          api("/api/playbook/histogram"),
          api("/api/playbook/stats?group=virtual"),
          api("/api/techniques/bonus"),
        ]);
        renderPlaybook(pb, hist, stats, bonus);
      } catch (e) {
        toast(e.message, "error");
      }
    },
  });

  // ━━ 소개 - 정적 문서(서버를 안 부른다). 새 기능을 추가하면 이 목록도 같이 손봐야 한다. ━━━━━
  const ABOUT_FEATURES = [
    "국내주식·해외주식(미국)·암호화폐(빗썸)·스윙(국내, 며칠~몇 주 보유) 4개 시장 자동매매(기본은 전부 모의매매/관찰)",
    "스윙은 일봉 기준 전용 기법(이동평균 눌림목·박스권 돌파·골든크로스) 3종을 쓰고, 종목선정도 테마 후보 + 스윙 관심 종목 중 중기 상승 추세(이동평균 위)만 남기는 별도 방식을 씀",
    "테마 자동 선정: 국내(동반 상승·거래대금 기준), 해외(같은 방식의 미국 테마), 암호화폐(전날 거래대금 상위)",
    "관심 종목은 테마와 별개로 항상 거래 대상에 포함",
    "규칙 기반 매매 기법 10여 종(돌파·눌림목·테마 대장주·시초 갭·장 막판 지속 등) - [매매 기법] 탭에서 근거·기준을 그대로 볼 수 있음",
    "위험 관리: 손절·익절·추적손절, 분할 매수(피라미딩)·분할 매도, 신호 강도·테마 근거·변동성에 따른 자동 사이징",
    "매매 속도 3단계(보통·빠른 단타·스캘핑, 스캘핑은 실험용)",
    "국내 종목 선정은 시장이 실제로 열려 있는 시간(프리마켓·정규장·NXT)에만 동작 - 야간·주말·공휴일에는 쉼",
    "AI 뉴스·공시 위험 필터(Groq, 선택) - 매수 직전 최근 헤드라인에 악재가 있으면 거름(실패하면 규칙 기반으로 대체)",
    "실험실: 여러 설정을 가상으로 비교(섀도·대조 실험), 종목별·테마별 매매 기법 백테스트(실제 최근 시세로 재생)",
    "일일·월간 매매 복기를 텔레그램으로 자동 발송, 실시간 알림(체결·중단 등)",
    "대시보드·성과·매매일지·종목선정 화면에서 지금 상태와 과거 기록을 확인",
    "새 기기 접속 승인 - 서버 PC에서만 여는 관리자 화면에서 승인해야 다른 기기가 접속 가능",
  ];
  const ABOUT_LIMITS = [
    "투자자문이 아닙니다 - 매매 여부·손익에 대한 책임은 전적으로 사용자에게 있습니다.",
    "기본값은 모의매매(paper)입니다. 실거래(live)로 바꾸는 것은 사용자의 선택이며, 반드시 모의매매로 충분히 검증한 뒤 바꾸세요.",
    "실험실·백테스트의 시뮬레이션 결과는 실제 시장 결과를 보장하지 않습니다 - 표본이 적고 슬리피지·유동성 등 실제 변수를 다 반영하지 못합니다.",
    "종목별 매매 기법 백테스트는 청산 방식을 단순화(공통 손절·익절만 사용)했고, 테마 대장주처럼 여러 종목을 동시에 봐야 하는 기법은 정확히 재현하지 못합니다.",
    "프리마켓(NXT) 시간대의 실제 매수·매도는 지원하지 않습니다(시세·주문 지원을 확인하지 못함).",
    "스윙 실거래(live)는 아직 지원하지 않습니다 - 관찰(web)·모의매매(paper)만 가능하며, 재시작 시 계좌 잔고와 대조해 복구하는 안전장치도 아직 없습니다.",
    "증권사·거래소 API(토스·빗썸) 상태, 인터넷 연결, 키 등록 여부에 따라 시세·주문이 지연되거나 실패할 수 있습니다.",
    "AI 뉴스 필터는 참고용 보조 장치일 뿐입니다 - 놓친 악재가 있을 수 있고, 키가 없거나 실패하면 조용히 규칙 기반으로 넘어갑니다.",
    "이 프로그램은 사용자의 PC에서 직접 돕니다 - 컴퓨터가 꺼지거나 프로그램이 종료되면 매매도 멈춥니다.",
    "비밀번호·API 키 관리, 새 기기 승인 등 보안 설정은 사용자가 직접 관리해야 합니다.",
    "국내·해외·암호화폐 모두 세금·수수료 등 실제 비용 구조가 바뀌면 설정을 함께 갱신해야 정확합니다.",
  ];

  // ★★★ "심플&모던, 스크롤 많은 네비게이션 지양" - 예전엔 소개 문단 아래로 기능
  // 13개·제한사항 11개 목록이 그대로 이어져 첫 화면부터 계속 스크롤해야 했다.
  // 첫 화면은 짧은 요약 카드만 두고, 두 목록은 탭으로 나눠 한 번에 하나만 보여준다.
  function renderAbout() {
    const panel = $('.panel[data-panel="about"]');
    panel.innerHTML = "";
    panel.appendChild(_pcIconEl("h2", "info", "AutoDayTrading 소개"));
    panel.appendChild(el("div", { class: "card" }, [
      el("p", {
        text: "AutoDayTrading 은 국내주식·해외(미국)주식·암호화폐(빗썸) 세 시장에서 테마 종목을 자동으로 골라, "
          + "규칙 기반 매매 기법으로 진입·청산을 판단하고 실행하는 개인용 자동매매 프로그램입니다. "
          + "사용자의 PC에서 직접 돌아가며, 기본값은 모의매매(가상 체결)이고 원할 때만 실거래로 바꿀 수 있습니다.",
      }),
      el("p", {
        text: "\"왜 샀는지·왜 안 샀는지\"를 항상 화면에 문장으로 남기는 것을 원칙으로 삼아, "
          + "블랙박스가 아니라 근거를 확인하며 다듬어 갈 수 있는 도구를 지향합니다.",
      }),
    ]));

    renderTabs(panel, "about", [
      {
        id: "features", label: `기능 (${ABOUT_FEATURES.length})`,
        build: () => el("ul", {}, ABOUT_FEATURES.map((t) => el("li", { text: t, style: { marginBottom: "6px" } }))),
      },
      {
        id: "limits", label: `제한사항·주의 (${ABOUT_LIMITS.length})`,
        build: () => el("ul", {}, ABOUT_LIMITS.map((t) => el("li", { text: t, style: { marginBottom: "6px" } }))),
      },
    ]);
  }

  registerPanel("about", { onShow: renderAbout });

  // ━━ 매매원칙 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function renderRules(doc) {
    const panel = $('.panel[data-panel="rules"]');
    panel.innerHTML = "";
    panel.appendChild(el("div", {
      class: "hint",
      text: `[${MODE_LABELS[doc.mode] || doc.mode}] 배정 ${won(doc.allocation)} · 본전 상승률 ${pct(doc.breakeven_pct, 3)}`,
    }));

    // ★ 9개 섹션이 한 페이지에 쌓여 2,500px 가 넘었다 - 섹션마다 탭으로 나눈다.
    renderTabs(panel, "rules", (doc.sections || []).map((sec, i) => ({
      id: sec.id || String(i), label: sec.title,
      build: () => {
        const box = el("div");
        if (sec.lead) box.appendChild(el("div", { text: sec.lead }));
        const ul = el("ul");
        (sec.items || []).forEach((it) => ul.appendChild(el("li", { html: `<b>${esc(it.head)}</b>: ${esc(it.body)}` })));
        box.appendChild(ul);
        if (sec.table) {
          const gridBox = el("div");
          new DataGrid(gridBox, {
            columns: [{ key: "k", label: "항목" }, { key: "v", label: "값" }, { key: "why", label: "왜" }],
            rows: sec.table, page: 0, storageKey: "grid.rules." + (sec.id || i),
          });
          box.appendChild(gridBox);
        }
        return box;
      },
    })));
  }

  registerPanel("rules", {
    onShow: async () => {
      try {
        renderRules(await api("/api/principles"));
      } catch (e) {
        toast(e.message, "error");
      }
    },
  });

  // ━━ 릴리즈 노트 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  // ★ **굵게**·`코드` 만 처리하는 최소 렌더러 - 텍스트는 전부 textContent 로
  //   넣어서(innerHTML 안 씀) 노트 내용이 무엇이든 화면을 깨거나 스크립트가
  //   실행될 수 없다.
  function _inlineNodes(text) {
    const frag = document.createDocumentFragment();
    String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/).forEach((part) => {
      if (!part) return;
      if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
        frag.appendChild(el("b", { text: part.slice(2, -2) }));
      } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
        frag.appendChild(el("code", { text: part.slice(1, -1) }));
      } else {
        frag.appendChild(document.createTextNode(part));
      }
    });
    return frag;
  }

  // ★★★ "최신 버전은 펼쳐서, 지난 버전은 접어서" - 예전엔 모든 버전이 카드로
  // 나열돼 펼쳐진 채라 지난 버전을 찾으려면 한참 스크롤해야 했다. 버전마다
  // details.acc 로 감싸 최신 것만 기본으로 열어 둔다.
  function renderReleaseNotes(data) {
    const panel = $('.panel[data-panel="release"]');
    panel.innerHTML = "";
    panel.appendChild(_pcIconEl("h2", "calendar", "릴리즈 노트"));
    const md = (data && data.markdown) || "";
    if (!md.trim()) {
      panel.appendChild(el("div", { class: "hint", text: "릴리즈 노트를 찾지 못했습니다." }));
      return;
    }

    let body = null;
    let list = null;
    let item = null;
    let firstDetails = null;
    const olderDetails = [];  // ★ 최신 버전을 뺀 나머지 - scroll-box 하나에 몰아넣는다.
    const intro = [];
    md.split(/\r?\n/).forEach((raw) => {
      const line = raw.replace(/\s+$/, "");
      let m;
      if (/^---+$/.test(line.trim()) || !line.trim()) {
        item = null;
        return;
      }
      if ((m = line.match(/^##\s+(.+)$/))) {
        const isFirst = !firstDetails;
        const details = el("details", { class: "acc", open: isFirst });
        const summary = el("summary", { text: m[1] });
        if (data.version && m[1].replace(/^v/i, "").trim() === data.version) {
          summary.appendChild(el("span", { class: "acc-meta", text: "현재 버전" }));
        }
        details.appendChild(summary);
        body = el("div", { class: "acc-body" });
        details.appendChild(body);
        if (isFirst) firstDetails = details; else olderDetails.push(details);
        list = null; item = null;
        return;
      }
      if (line.startsWith("# ")) return;
      if (!body) { intro.push(line.trim()); return; }
      if ((m = line.match(/^###\s+(.+)$/))) {
        body.appendChild(el("b", { text: m[1], style: { display: "block", margin: "var(--s2) 0 4px" } }));
        list = null; item = null;
        return;
      }
      if ((m = line.match(/^-\s+(.*)$/))) {
        if (!list) { list = el("ul", { style: { margin: "0 0 var(--s2) 1.2em", padding: 0 } }); body.appendChild(list); }
        item = el("li", { style: { marginBottom: "6px" } });
        item.appendChild(_inlineNodes(m[1]));
        list.appendChild(item);
        return;
      }
      if (item && /^\s+/.test(raw)) {
        item.appendChild(document.createTextNode(" "));
        item.appendChild(_inlineNodes(line.trim()));
        return;
      }
      body.appendChild(el("div", { class: "hint" }, [_inlineNodes(line.trim())]));
    });
    if (intro.length) {
      panel.appendChild(el("div", { class: "hint", style: { marginBottom: "var(--s3)" } }, [_inlineNodes(intro.join(" "))]));
    }
    if (firstDetails) panel.appendChild(firstDetails);
    // ★★★ "최신은 펼치고 지난 것은 접어서" 만으로도 스크롤은 줄지만, 버전이 수십 개면
    // 접힌 한 줄짜리 항목만 해도 페이지가 길어진다 - 지난 버전 전체를 scroll-box 하나에
    // 몰아 페이지 자체 길이는 짧게, 옛 버전을 찾을 때만 그 상자 안에서 스크롤하게 한다.
    if (olderDetails.length) {
      const olderWrap = el("details", { class: "acc" });
      olderWrap.appendChild(el("summary", { text: `지난 버전 (${olderDetails.length}개)` }));
      const box = el("div", { class: "acc-body scroll-box" });
      olderDetails.forEach((d) => box.appendChild(d));
      olderWrap.appendChild(box);
      panel.appendChild(olderWrap);
    }
  }

  registerPanel("release", {
    onShow: async () => {
      try {
        renderReleaseNotes(await api("/api/changelog"));
      } catch (e) {
        toast(e.message, "error");
      }
    },
  });

  // ━━ 준비·연결 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function renderSetupStatus(setup) {
    return el("div", {
      class: "card",
      html: `모드: <b>${esc(MODE_LABELS[setup.mode] || setup.mode)}</b> · 테마 ${setup.theme_count}개 · ` +
        `종목 ${setup.symbol_count}개 · API 키: ${setup.has_keys ? "등록됨" : "없음"}`,
    });
  }

  function renderConnCheck(diag) {
    const wrap = el("div", { class: "card" });
    wrap.appendChild(el("div", { html: `<b>접속 방식</b>: ${esc(diag.mode_label || diag.mode || "-")}` }));
    const gridBox = el("div");
    new DataGrid(gridBox, {
      columns: [
        { key: "label", label: "대상" },
        { key: "ok", label: "결과", fmt: (v) => (v ? "✅" : "⛔") },
        { key: "status", label: "HTTP" },
        { key: "ms", label: "응답시간(ms)" },
        { key: "detail", label: "설명" },
      ],
      rows: diag.targets || [], page: 0, storageKey: "grid.setup.diagnose",
    });
    wrap.appendChild(gridBox);
    return wrap;
  }

  function renderStepsHowTo(title, steps) {
    const details = el("details");
    details.appendChild(el("summary", { text: title }));
    const ol = el("ol");
    steps.forEach((s) => ol.appendChild(el("li", { html: s })));
    details.appendChild(ol);
    return details;
  }

  function _scrollBox(node) {
    // ★★★ "설명란이 너무 길다 - 가로 스크롤 넣고 현재 창에서 볼 수 있게"
    // 연계 테스트 표의 '실패 이유'·'받은 값'은 API 원본 메시지라 길어질
    // 수밖에 없다. 표를 가로 스크롤 컨테이너로 감싸서, 창을 벗어나지 않고
    // 표 안에서만 좌우로 움직이게 한다.
    const box = el("div", { class: "hscroll" });
    box.appendChild(node);
    return box;
  }

  function renderTossTestResult(result) {
    const box = el("div");
    box.appendChild(el("div", { class: result.ok ? "banner" : "banner danger", text: result.summary }));
    const gridBox = el("div");
    new DataGrid(gridBox, {
      columns: [
        { key: "mark", label: "" },
        { key: "label", label: "항목" },
        { key: "required", label: "필수/선택" },
        { key: "detail", label: "받은 값" },
        { key: "error", label: "실패 이유" },
        { key: "ms", label: "ms" },
      ],
      rows: (result.steps || []).map((s) => ({
        mark: s.ok === true ? "✅" : s.ok === false ? (s.required ? "⛔" : "⚠️") : "—",
        label: s.label, required: s.required ? "필수" : "선택",
        detail: s.detail == null ? "" : (typeof s.detail === "object" ? JSON.stringify(s.detail) : String(s.detail)),
        error: s.error || "", ms: s.ms == null ? "" : s.ms,
        why: s.why,
      })),
      expand: (row) => el("div", { text: row.why }),
      page: 0, storageKey: "grid.toss.test",
    });
    box.appendChild(_scrollBox(gridBox));
    box.appendChild(el("div", { class: "hint", text: result.note }));
    return box;
  }

  function _statusBadge(label, ok) {
    // ★ "등록됨"과 "미등록"이 색 구분 없이 똑같은 회색 배지라 눈에 안
    // 띄었다. 상태에 따라 색을 확실히 다르게 준다.
    return `<span class="badge tech" style="cursor:default;background:${ok ? "var(--rise)" : "var(--rule2)"};` +
      `color:${ok ? "#fff" : "var(--muted)"};border-color:transparent;">${label}</span>`;
  }

  // 자격증명 입력 한 줄: "항목 이름 + 등록 상태" 라벨을 입력칸 위에 항상 보여 준다(placeholder 는 입력하면 사라진다).
  // 저장된 값은 화면에 다시 채우지 않는다 - 식별자(ID·Access Key·채팅 ID)만 끝 4자리, 비밀값은 "등록됨"만.
  function _credRow(label, isSet, tail, input, note) {
    const row = el("div", { class: "cred-row" });
    const head = el("div", { class: "cred-label" });
    head.appendChild(el("b", { text: label }));
    head.insertAdjacentHTML("beforeend", " " + _statusBadge(isSet ? (tail ? `등록됨 · …${esc(tail)}` : "등록됨") : "미등록", isSet));
    if (note) head.appendChild(el("span", { class: "hint", text: " " + note }));
    row.appendChild(head);
    row.appendChild(input);
    return row;
  }

  function renderTossSection(integrations, onSaved) {
    const toss = integrations.toss;
    const card = el("div", { class: "card" });
    const titleRow = el("div");
    titleRow.innerHTML = `<b>① 토스증권 Open API</b> ` +
      _statusBadge(toss.configured ? "등록됨" : "미등록", toss.configured) +
      (toss.needed ? ' <span class="badge tech" style="cursor:default;background:var(--halt);color:#fff;border-color:transparent;">필수</span>' : "");
    card.appendChild(titleRow);

    card.appendChild(renderStepsHowTo("발급 방법 보기", [
      "토스증권 PC 웹(WTS)에 로그인합니다. 모바일 앱에는 이 메뉴가 없습니다.",
      "오른쪽 위 설정 → Open API 로 들어갑니다.",
      "앱 등록을 하면 <b>client_id</b> 와 <b>client_secret</b> 이 발급됩니다.",
      "같은 화면의 '허용 IP 관리'에 이 PC의 공인 IP를 등록합니다. "
        + "<b>등록하지 않으면 키가 맞아도 전부 거부됩니다.</b> (IP가 유동적이면 바뀔 때마다 다시 등록해야 합니다.)",
      "저장한 뒤 아래 연계 테스트로 항목별로 확인합니다.",
    ]));

    const warnRow = el("div", { class: "banner warn" });
    warnRow.appendChild(_pcIconEl("span", "alert", "client_secret 은 발급 화면을 닫으면 다시 볼 수 없습니다 - 먼저 적어 두세요."));
    warnRow.appendChild(infoIcon(
      "이 프로그램은 두 값을 exe 옆 secrets.yaml 에 이 PC·이 Windows 계정에서만 풀 수 있게 암호화해 저장하고 "
      + "어디로도 전송하지 않습니다. 이 파일을 남에게 주거나 버전관리에 올리지 마세요."));
    card.appendChild(warnRow);

    // ★ 보안상 저장된 값은 절대 화면에 다시 채우지 않는다 - 그래서 입력칸은
    // 늘 비어 있다. 그것만 보고 "저장이 안 됐다"고 오해하지 않도록, 지금
    // 등록 상태를 placeholder 로 분명히 알려준다.
    const idInput = el("input", {
      type: "text", autocomplete: "off",
      placeholder: toss.client_id_set ? "바꾸려면 새 Client ID 입력" : "Client ID 입력",
    });
    const secretInput = el("input", {
      type: "password", autocomplete: "off",
      placeholder: toss.client_secret_set ? "바꾸려면 새 Client Secret 입력" : "Client Secret 입력",
    });
    card.appendChild(_credRow("Client ID", toss.client_id_set, toss.client_id_tail, idInput));
    card.appendChild(_credRow("Client Secret", toss.client_secret_set, "", secretInput, "(비밀값 - 화면에 다시 보이지 않습니다)"));
    const saveStatus = el("span", { class: "hint" });
    card.appendChild(el("button", {
      class: "b", text: "저장",
      onclick: async () => {
        if (!idInput.value.trim() || !secretInput.value.trim()) {
          toast("client_id 와 client_secret 을 모두 입력하세요.", "error");
          return;
        }
        try {
          await api("/api/credentials", { method: "POST", body: { client_id: idInput.value.trim(), client_secret: secretInput.value.trim() } });
          toast("저장했습니다.");
          saveStatus.textContent = "✓ 방금 저장했습니다.";
          saveStatus.className = "hint rise";
          if (onSaved) onSaved();
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }));
    card.appendChild(saveStatus);

    card.appendChild(el("h2", { text: "연계 테스트" }));
    const symbolInput = el("input", { type: "text", value: "005930", placeholder: "종목코드" });
    const resultBox = el("div");
    card.appendChild(symbolInput);
    card.appendChild(el("button", {
      class: "b ghost", text: "연계 테스트",
      onclick: async () => {
        busy(resultBox, true);
        try {
          const result = await api("/api/toss/test", { method: "POST", body: { symbol: symbolInput.value.trim() || "005930" } });
          fill(resultBox, renderTossTestResult(result));
        } catch (e) {
          toast(e.message, "error");
        } finally {
          busy(resultBox, false);
        }
      },
    }));
    card.appendChild(el("span", { class: "hint", text: " ★ 조회만 하므로 계좌에 아무 변화도 없습니다." }));

    // ★★★ "연계 테스트는 통과하는데 종목선정만 시세를 못 받는다"는 문제를
    // 끝내기 위한 진단. 두 경로는 호출 조건이 다른데(연계 테스트는
    // count=10, 스크리닝은 count=100 + TOP_GAINERS 도 호출) 그 차이를
    // 눈으로 볼 방법이 없었다. 실제 응답을 그대로 보여준다.
    const diagBox = el("div");
    card.appendChild(el("button", {
      class: "b ghost small", text: "종목선정 시세 진단",
      onclick: async () => {
        busy(diagBox, true);
        try {
          const d = await api("/api/debug/rankings");
          const box = el("div");
          box.appendChild(el("div", { class: "hint", text: `모드 ${d.mode} · 스크리닝 조회 수 ${d.ranking_count}` }));

          (d.cases || []).forEach((c) => {
            box.appendChild(el("div", {
              class: c.ok ? "hint" : "hint fall",
              text: `${c.ok ? "\u2705" : "\u26D4"} ${c.label} \u2192 `
                + (c.ok ? `${c.length}건 받음` : c.error),
            }));
            if (c.symbols && c.symbols.length) {
              box.appendChild(el("div", {
                class: "hint", style: { paddingLeft: "var(--s4)" },
                text: "\u21B3 " + c.symbols.join(", "),
              }));
            }
          });

          // ★★★ 핵심 판정 - 받은 건수와 파싱 통과 건수를 나란히 본다.
          const got = (d.cases || [])[0] || {};
          const same = got.length === d.parsed_count;
          box.appendChild(el("div", {
            class: same ? "hint rise" : "hint fall",
            style: { marginTop: "var(--s2)" },
            text: `${same ? "\u2705" : "\u26D4"} 받은 ${got.length ?? "-"}건 중 파싱 통과 ${d.parsed_count}건`
              + (same ? " - 프로그램 파싱은 정상입니다." : " - 파싱에서 걸러지고 있습니다."),
          }));

          const tr = d.parse_trace;
          if (Array.isArray(tr) && tr.length) {
            box.appendChild(el("div", { html: `<b>파싱 추적(${tr.length}건)</b>`, style: { marginTop: "var(--s2)" } }));
            tr.forEach((t, i) => {
              const ok = t.passes_filter && t.price != null;
              box.appendChild(el("div", {
                class: ok ? "hint" : "hint fall",
                text: `${i + 1}. ` + (t.is_dict === false
                  ? `딕셔너리가 아님 \u2192 ${JSON.stringify(t)}`
                  : `${t.symbol_raw} · 통과=${t.passes_filter} · price=${t.price} · amount=${t.tradingAmount}`),
              }));
            });
          }

          box.appendChild(el("div", { class: "hint", style: { marginTop: "var(--s2)" }, text: d.note || "" }));
          fill(diagBox, box);
        } catch (e) {
          toast(e.message, "error");
        } finally {
          busy(diagBox, false);
        }
      },
    }));
    card.appendChild(diagBox);
    card.appendChild(resultBox);

    return card;
  }

  function renderTelegramSection(integrations, onSaved) {
    const tg = integrations.telegram;
    const card = el("div", { class: "card" });
    const titleRow = el("div");
    titleRow.innerHTML = `<b>③ 텔레그램 알림</b> ` +
      _statusBadge(tg.configured ? "등록됨" : "미등록", tg.configured) + " " +
      _statusBadge(tg.enabled ? "자동 알림 켜짐" : "자동 알림 꺼짐", tg.enabled);
    card.appendChild(titleRow);
    if (!tg.enabled) card.appendChild(el("div", { class: "hint", text: tg.why_off }));
    card.appendChild(el("div", { class: "hint", text: `보냄 ${tg.sent} · 실패 ${tg.failed}` + (tg.last_error ? ` · 마지막 오류: ${tg.last_error}` : "") }));

    card.appendChild(renderStepsHowTo("발급 방법 보기", [
      "텔레그램에서 @BotFather 를 찾아 대화를 엽니다.",
      "/newbot 을 보내고 이름과 사용자명을 정합니다 (사용자명은 bot 으로 끝나야 합니다).",
      "봇 토큰을 받습니다 — 숫자:영문자 형태입니다.",
      "<b>방금 만든 봇을 검색해 아무 말이나 한 번 보냅니다.</b> 이걸 안 하면 봇이 나에게 "
        + "말을 걸 수 없습니다 — 가장 많이 빠뜨리는 단계입니다.",
      "@userinfobot 에게 말을 걸어 Id(채팅 ID)를 확인합니다.",
      "저장하고 테스트 전송을 눌러 확인합니다.",
    ]));

    const tgWarn = el("div", { class: "banner warn" });
    tgWarn.appendChild(_pcIconEl("span", "alert", "봇 토큰은 secrets.yaml 에 암호화되어 저장됩니다 - 그래도 토큰이 노출되면 폐기하세요."));
    tgWarn.appendChild(infoIcon(
      "토큰이 노출되면 다른 사람이 이 봇으로 메시지를 보낼 수 있습니다.\n\n"
      + "노출됐다면 텔레그램에서 @BotFather 를 찾아 /revoke 명령으로 토큰을 폐기하고 "
      + "새로 발급받아 다시 등록하세요."));
    card.appendChild(tgWarn);

    // ★ 여기도 보안상 저장된 값을 다시 채우지 않는다 - placeholder 로 상태를 알린다.
    const tokenInput = el("input", {
      type: "password", autocomplete: "off",
      placeholder: tg.token_set ? "바꾸려면 새 봇 토큰(Bot Token) 입력" : "봇 토큰(Bot Token) 입력",
    });
    const chatIdInput = el("input", {
      type: "text", autocomplete: "off",
      placeholder: tg.chat_id_set ? "바꾸려면 새 채팅 ID 입력" : "채팅 ID(Chat ID) 입력",
    });
    card.appendChild(_credRow("봇 토큰 (Bot Token)", tg.token_set, "", tokenInput, "(비밀값 - 화면에 다시 보이지 않습니다)"));
    card.appendChild(_credRow("채팅 ID (Chat ID)", tg.chat_id_set, tg.chat_id_tail, chatIdInput));
    const saveStatus = el("span", { class: "hint" });
    card.appendChild(el("button", {
      class: "b", text: "저장",
      onclick: async () => {
        if (!tokenInput.value.trim() || !chatIdInput.value.trim()) {
          toast("봇 토큰과 채팅 ID 를 모두 입력하세요.", "error");
          return;
        }
        try {
          await api("/api/notify/credentials", { method: "POST", body: { token: tokenInput.value.trim(), chat_id: chatIdInput.value.trim() } });
          toast("저장했습니다.");
          saveStatus.textContent = "✓ 방금 저장했습니다.";
          saveStatus.className = "hint rise";
          if (onSaved) onSaved();
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }));
    card.appendChild(saveStatus);

    const testRow = el("div", { style: { marginTop: "var(--s2)", display: "flex", gap: "var(--s2)", flexWrap: "wrap" } });
    testRow.appendChild(el("button", {
      class: "b ghost small", text: "테스트 전송",
      onclick: async () => {
        // ★ 입력칸에 값이 있으면 저장 전 값으로 시험한다. 비어 있으면 저장된 값을 쓴다.
        const token = tokenInput.value.trim();
        const chatId = chatIdInput.value.trim();
        try {
          const result = await api("/api/notify/test", { method: "POST", body: { token, chat_id: chatId } });
          toast(result.message || "테스트 메시지를 보냈습니다.");
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }));
    ["현황", "오늘", "이번 달", "올해"].forEach((label, i) => {
      const kind = ["now", "daily", "monthly", "yearly"][i];
      testRow.appendChild(el("button", {
        class: "b ghost small", text: "요약 보내기: " + label,
        onclick: async () => {
          try {
            await api("/api/notify/send", { method: "POST", body: { kind } });
            toast("보냈습니다.");
          } catch (e) {
            toast(e.message, "error");
          }
        },
      }));
    });
    card.appendChild(testRow);

    // 일일 복기: 장이 끝난 뒤 자동으로 오고(국내 15:40 · 미국 한국시간 06:00 · 암호화폐 21:00), 여기서 지금 받아 볼 수도 있다.
    const reviewRow = el("div", { style: { marginTop: "var(--s2)", display: "flex", gap: "var(--s2)", flexWrap: "wrap", alignItems: "center" } });
    reviewRow.appendChild(el("span", { class: "hint", text: "일일 복기 지금 보내기:" }));
    [["domestic", "🇰🇷 국내"], ["overseas", "🌍 해외"], ["crypto", "🪙 암호화폐"]].forEach(([market, label]) => {
      reviewRow.appendChild(el("button", { class: "b ghost small", text: label, onclick: async () => {
        try {
          await api("/api/review/daily/send", { method: "POST", body: { market } });
          toast("복기를 텔레그램으로 보냈습니다.");
        } catch (e) { toast(e.message, "error"); }
      } }));
    });
    card.appendChild(reviewRow);

    return card;
  }

  // ④ AI 뉴스·공시 위험 필터(Groq 키 2개) - 사기 직전에 종목의 최근 헤드라인을 읽어 악재가 있으면 제외한다.
  function renderLlmSection(integrations, onSaved) {
    const llm = integrations.llm || {};
    const card = el("div", { class: "card" });
    const titleRow = el("div");
    titleRow.innerHTML = `<b>④ AI 뉴스·공시 위험 필터 (Groq)</b> ` + _statusBadge(llm.configured ? "키 등록됨" : "키 없음(선택)", llm.configured);
    card.appendChild(titleRow);
    card.appendChild(el("div", { class: "hint",
      text: "Groq 키를 등록하면 사기 직전에 그 종목의 최근 헤드라인을 AI 가 읽고, 상장폐지·횡령·유상증자·소송 같은 악재가 있으면 사지 않습니다. "
        + "키 1의 무료 한도가 차거나 오류가 나면 키 2로 넘어가고, 둘 다 안 되면 지금처럼 키워드(규칙) 기반으로 판단합니다. "
        + "AI 는 거르기에만 쓰고(사라고 하지 않음), 종목 이름과 공개 헤드라인만 보냅니다(계좌·금액·다른 키는 보내지 않음). "
        + "무료 플랜은 요청 속도(분당 횟수) 제한이 있어 키 2를 백업으로 둡니다. 발급: console.groq.com 에서 API 키 만들기(가입만 하면 무료로 발급됩니다)." }));
    const k1 = el("input", { type: "password", autocomplete: "off", placeholder: llm.groq_set ? "바꾸려면 새 키 입력" : "Groq API 키 1" });
    const k2 = el("input", { type: "password", autocomplete: "off", placeholder: llm.groq2_set ? "바꾸려면 새 키 입력" : "Groq API 키 2 (백업)" });
    card.appendChild(_credRow("Groq 키 1", !!llm.groq_set, "", k1, "(비밀값 - 화면에 다시 보이지 않습니다)"));
    card.appendChild(_credRow("Groq 키 2 (백업)", !!llm.groq2_set, "", k2, "(1번 한도가 차면 사용)"));
    const status = el("span", { class: "hint" });
    const row = el("div", { style: { display: "flex", gap: "var(--s2)", flexWrap: "wrap", marginTop: "var(--s2)" } });
    row.appendChild(el("button", { class: "b", text: "저장", onclick: async () => {
      const body = {};
      if (k1.value.trim()) body.groq_key = k1.value.trim();
      if (k2.value.trim()) body.groq_key2 = k2.value.trim();
      if (!Object.keys(body).length) { toast("바꿀 키를 입력하세요.", "error"); return; }
      try {
        await api("/api/llm/credentials", { method: "POST", body });
        toast("저장했습니다.");
        if (onSaved) onSaved();
      } catch (e) { toast(e.message, "error"); }
    } }));
    [["groq_key", "키 1 지우기", llm.groq_set], ["groq_key2", "키 2 지우기", llm.groq2_set]].forEach(([field, label, isSet]) => {
      if (!isSet) return;
      row.appendChild(el("button", { class: "b ghost small", text: label, onclick: async () => {
        try { await api("/api/llm/credentials", { method: "POST", body: { [field]: "" } }); toast("지웠습니다."); if (onSaved) onSaved(); }
        catch (e) { toast(e.message, "error"); }
      } }));
    });
    row.appendChild(el("button", { class: "b ghost small", text: "연결 테스트", onclick: async () => {
      try {
        const r = await api("/api/llm/test", { method: "POST", body: {} });
        toast(r.results.map((x) => `${x.label}: ${x.ok ? "정상" : "실패(" + x.error + ")"}`).join(" / "));
      } catch (e) { toast(e.message, "error"); }
    } }));
    row.appendChild(el("button", { class: "b ghost small", text: "지금 상태 보기", onclick: async () => {
      try {
        const g = await api("/api/news/guard");
        const blocked = (g.recent || []).filter((r) => r.level === "block");
        status.textContent = `AI ${g.ai_enabled ? "켜짐" : "꺼짐"}${g.provider ? " (마지막 사용: " + g.provider + ")" : ""} · 오늘 호출 ${g.calls_today}/${g.daily_cap} · 최근 제외 ${blocked.length}건`
          + (blocked.length ? " — " + blocked.slice(0, 3).map((r) => `${r.symbol}(${r.reason})`).join(", ") : "")
          + (g.last_error ? ` · 마지막 오류: ${g.last_error}` : "");
      } catch (e) { toast(e.message, "error"); }
    } }));
    card.appendChild(row);
    card.appendChild(status);
    return card;
  }

  function renderBithumbSection(integrations, onSaved) {
    const bithumb = integrations.bithumb;
    const card = el("div", { class: "card" });
    const titleRow = el("div");
    titleRow.innerHTML = `<b>② 빗썸(암호화폐) Open API</b> ` +
      _statusBadge(bithumb.configured ? "등록됨" : "미등록", bithumb.configured);
    card.appendChild(titleRow);
    card.appendChild(el("div", { class: "hint", text: "아직 자동매매 로직은 없습니다 - 연계 테스트와 시세 관찰만 가능합니다." }));

    card.appendChild(renderStepsHowTo("발급 방법 보기", [
      "빗썸에 로그인한 뒤 마이페이지 → Open API 관리로 들어갑니다.",
      "API 키 발급을 누르면 <b>Access Key</b>와 <b>Secret Key</b>가 발급됩니다.",
      "필요한 권한(자산 조회, 주문 조회 등)에 체크합니다.",
      "저장한 뒤 아래 연계 테스트로 항목별로 확인합니다.",
    ]));

    const warnRow = el("div", { class: "banner warn" });
    warnRow.appendChild(_pcIconEl("span", "alert", "Secret Key 는 발급 화면을 닫으면 다시 볼 수 없습니다 - 먼저 적어 두세요."));
    warnRow.appendChild(infoIcon(
      "이 프로그램은 두 값을 exe 옆 secrets.yaml 에 이 PC·이 Windows 계정에서만 풀 수 있게 암호화해 저장하고 "
      + "어디로도 전송하지 않습니다. 이 파일을 남에게 주거나 버전관리에 올리지 마세요."));
    card.appendChild(warnRow);

    const akInput = el("input", {
      type: "text", autocomplete: "off",
      placeholder: bithumb.access_key_set ? "바꾸려면 새 Access Key 입력" : "Access Key 입력",
    });
    const skInput = el("input", {
      type: "password", autocomplete: "off",
      placeholder: bithumb.secret_key_set ? "바꾸려면 새 Secret Key 입력" : "Secret Key 입력",
    });
    card.appendChild(_credRow("Access Key", bithumb.access_key_set, bithumb.access_key_tail, akInput));
    card.appendChild(_credRow("Secret Key", bithumb.secret_key_set, "", skInput, "(비밀값 - 화면에 다시 보이지 않습니다)"));
    const saveStatus = el("span", { class: "hint" });
    card.appendChild(el("button", {
      class: "b", text: "저장",
      onclick: async () => {
        if (!akInput.value.trim() || !skInput.value.trim()) {
          toast("Access Key 와 Secret Key 를 모두 입력하세요.", "error");
          return;
        }
        try {
          await api("/api/bithumb/credentials", { method: "POST", body: { access_key: akInput.value.trim(), secret_key: skInput.value.trim() } });
          toast("저장했습니다.");
          saveStatus.textContent = "✓ 방금 저장했습니다.";
          saveStatus.className = "hint rise";
          if (onSaved) onSaved();
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }));
    card.appendChild(saveStatus);

    card.appendChild(el("h2", { text: "연계 테스트" }));
    const marketInput = el("input", { type: "text", value: "KRW-BTC", placeholder: "마켓 코드" });
    const resultBox = el("div");
    card.appendChild(marketInput);
    card.appendChild(el("button", {
      class: "b ghost", text: "연계 테스트",
      onclick: async () => {
        busy(resultBox, true);
        try {
          const result = await api("/api/bithumb/test", {
            method: "POST",
            body: { access_key: akInput.value.trim(), secret_key: skInput.value.trim(), market: marketInput.value.trim() || "KRW-BTC" },
          });
          fill(resultBox, renderTossTestResult(result));
        } catch (e) {
          toast(e.message, "error");
        } finally {
          busy(resultBox, false);
        }
      },
    }));
    card.appendChild(el("span", { class: "hint", text: " ★ 조회만 하므로 계좌에 아무 변화도 없습니다." }));
    card.appendChild(resultBox);

    return card;
  }

  function renderPasswordSection(integrations, onSaved) {
    // ★★★ Tailscale 등으로 외부에서 접속할 수 있게 되면서 추가한 화면
    // 잠금 비밀번호(숫자 6자리) - 로그인 자체는 서버가 지키고, 여기는
    // 그 비밀번호를 바꾸는 곳이다.
    const auth = integrations.auth || {};
    const card = el("div", { class: "card" });
    const titleRow = el("div");
    titleRow.innerHTML = `<b>④ 화면 잠금 비밀번호</b> ` +
      _statusBadge(auth.is_default_password ? "기본값(123456) 사용 중" : "사용자 지정", !auth.is_default_password);
    card.appendChild(titleRow);
    card.appendChild(el("div", {
      class: "hint",
      text: "이 프로그램을 열 때마다 물어보는 숫자 6자리입니다. Tailscale 등 바깥 네트워크에서 "
        + "접속할 수 있게 열어 뒀다면, 기본값 그대로 두지 말고 꼭 바꾸세요.",
    }));
    if (auth.is_default_password) {
      const warnRow = el("div", { class: "banner warn" });
      warnRow.appendChild(_pcIconEl("span", "alert", "아직 기본 비밀번호(123456)를 쓰고 있습니다 - 바깥에 열려 있다면 아무나 들어올 수 있습니다."));
      card.appendChild(warnRow);
    }

    const pwInput = el("input", {
      type: "password", inputmode: "numeric", pattern: "[0-9]*", maxlength: "6",
      autocomplete: "new-password", placeholder: "새 비밀번호 (숫자 6자리)",
    });
    card.appendChild(pwInput);
    const saveStatus = el("span", { class: "hint" });
    card.appendChild(el("button", {
      class: "b", text: "저장",
      onclick: async () => {
        const pw = pwInput.value.trim();
        if (!/^\d{6}$/.test(pw)) {
          toast("비밀번호는 숫자 6자리여야 합니다.", "error");
          return;
        }
        try {
          await api("/api/auth/password", { method: "POST", body: { password: pw } });
          toast("저장했습니다. 다른 곳에서 로그인해 둔 화면은 다시 로그인해야 합니다.");
          saveStatus.textContent = "✓ 방금 저장했습니다.";
          saveStatus.className = "hint rise";
          pwInput.value = "";
          const pwBanner = $("#default-pw-banner");
          if (pwBanner) pwBanner.remove();
          if (onSaved) onSaved();
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }));
    card.appendChild(saveStatus);

    card.appendChild(el("button", {
      class: "b ghost small", text: "로그아웃", style: { marginTop: "var(--s2)" },
      onclick: async () => {
        try {
          await api("/api/logout", { method: "POST" });
          _clearConfirmTokens();
        } catch (e) {
          // ★ 로그아웃은 실패해도 화면은 로그인 화면으로 보낸다 - 쿠키가
          // 이미 없어졌을 수도 있는 상황이라 사용자를 막을 이유가 없다.
        }
        showLogin();
      },
    }));

    return card;
  }

  function renderCredentialsForm(onSaved, prefetched) {
    // ★★★ "토스 API 를 등록하고 연결되면 자동으로 시세를 가져오고 메시지도
    // 바뀌어야 한다" - 예전엔 이 함수가 콜백을 받지 않아, 키를 저장해도
    // 자기 카드만 다시 그렸다. 그래서 바깥의 "미연계" 표시와 계좌 현황
    // 안내가 옛 상태로 남아 있었다. onSaved 를 받아 화면 전체를 갱신한다.
    const wrap = el("div");
    // ★★★ "설명 텍스트는 제목을 마우스 오버했을 때만" - 긴 안내를 본문에
    // 깔면 화면이 길어진다. 제목에 달아 두고 필요할 때만 보게 한다.
    wrap.appendChild(titleWithHelp("외부 연동",
      "이 프로그램이 바깥과 주고받는 것은 세 가지뿐입니다 — 토스증권(국내주식 시세·주문), "
      + "텔레그램(알림), 빗썸(암호화폐 시세·주문).\n\n"
      + "전부 없어도 연습 모드는 인터넷 공개 시세로 정상 동작합니다."));
    const body = el("div");
    wrap.appendChild(body);

    let seed = prefetched || null;  // ★ 바로 위에서 이미 받아 둔 값이 있으면 같은 API 를 또 부르지 않는다.
    function load() {
      fill(body, el("div", { class: "hint", text: "불러오는 중..." }));
      const src = seed ? Promise.resolve(seed) : api("/api/integrations");
      seed = null;
      src.then((integrations) => {
        // ★ 세 카드를 나란히 배치해 세로 길이를 줄인다.
        const cols = el("div", { class: "cols" });
        // ★ 키를 저장하면 이 카드뿐 아니라 화면 전체를 다시 그린다 -
        //   연결 상태·계좌 현황·안내 문구가 한꺼번에 갱신돼야 한다.
        const refresh = () => { if (onSaved) onSaved(); else load(); };
        cols.appendChild(renderTossSection(integrations, refresh));
        cols.appendChild(renderBithumbSection(integrations, refresh));
        cols.appendChild(renderTelegramSection(integrations, refresh));
        cols.appendChild(renderLlmSection(integrations, refresh));
        cols.appendChild(renderPasswordSection(integrations, refresh));
        fill(body, cols);
      }).catch((e) => {
        fill(body, el("div", { class: "hint", text: "불러오지 못했습니다: " + e.message }));
      });
    }
    load();

    return wrap;
  }

  function renderPreflight(pf) {
    const wrap = el("div", { class: "card" });
    if (!pf.ok) {
      wrap.appendChild(el("div", { class: "banner danger", text: "막힌 항목이 있어 실거래를 시작할 수 없습니다." }));
    }
    const gridBox = el("div");
    new DataGrid(gridBox, {
      columns: [
        { key: "icon", label: "" }, { key: "label", label: "항목" },
        { key: "api", label: "관련 API" }, { key: "detail", label: "설명" },
      ],
      rows: (pf.checks || []).map((c) => ({
        icon: c.ok ? "✅" : c.level === "block" ? "⛔" : "⚠️",
        label: c.label, api: c.api || "-", detail: c.detail,
      })),
      page: 0, storageKey: "grid.setup.preflight",
    });
    wrap.appendChild(gridBox);
    return wrap;
  }

  // ★ ok===null 이면 "아직 확인 전"(진단·사전 점검은 늦게 도착) - kpi-value 에
  // 색을 안 주고 대시로 둔다. ok 는 rise/fall 색 관례(빨강=좋음/파랑=나쁨)를
  // 그대로 따른다 - 이 화면 다른 곳(손익 등)과 색 의미가 어긋나지 않게 한다.
  function _cfgKpiTone(ok) {
    return ok === null ? "" : ok ? "rise" : "fall";
  }

  function _cfgKpiCard(iconName, label, ok, sub) {
    const kpi = el("div", { class: "kpi" });
    kpi.appendChild(_pcIconEl("div", iconName, label, { class: "kpi-label" }));
    const tone = _cfgKpiTone(ok);
    kpi.appendChild(el("div", { class: ("kpi-value " + tone).trim(), text: ok === null ? "-" : ok ? "연결됨" : "미연결" }));
    if (sub) kpi.appendChild(el("div", { class: "kpi-sub", text: sub }));
    return kpi;
  }

  function renderSetupOverview(setup, integrations, diag, pf) {
    // ★★ 아래에 "연계 테스트"(각 서비스 카드) · "연결 진단"(네트워크) ·
    // "사전 점검"(실거래 직전 체크)이 전부 계좌·인증 관련 항목을 조금씩
    // 겹쳐서 보여줘 혼란스럽다는 지적이 있었다 - 자세히 안 봐도 되게
    // 맨 위에 한눈에 보기 요약(kpi 카드)을 두고, 나머지는 탭·접힌 항목으로 미룬다.
    const wrap = el("div");
    wrap.appendChild(_pcIconEl("h2", "shield", "한눈에 보기"));
    const netOk = diag && diag.targets && diag.targets.length ? diag.targets.every((t) => t.ok) : null;
    const grid = el("div", { class: "kpi-grid" });
    grid.appendChild(_cfgKpiCard("plug", "토스증권", integrations.toss.configured));
    grid.appendChild(_cfgKpiCard("plug", "빗썸", integrations.bithumb.configured));
    grid.appendChild(_cfgKpiCard("message", "텔레그램", integrations.telegram.configured));
    grid.appendChild(_cfgKpiCard("flask", "Groq(AI 뉴스 필터)", !!(integrations.llm && integrations.llm.configured)));
    grid.appendChild(_cfgKpiCard("wifi", "네트워크", netOk));
    grid.appendChild(_cfgKpiCard("shield", "실거래 사전 점검", pf ? pf.ok : null));
    wrap.appendChild(grid);
    const guideRow = el("div", { class: "hint", style: { marginTop: "var(--s2)" } });
    guideRow.appendChild(el("span", { text: "더 보기" }));
    guideRow.appendChild(infoIcon(
      "자세한 내용은 [외부 연동] 탭의 '연계 테스트'와, "
      + "[진단·점검] 탭의 네트워크 연결 진단·실거래 사전 점검에서 볼 수 있습니다."));
    wrap.appendChild(guideRow);
    return wrap;
  }

  function _accountHoldingsTable(rows, priceUnit) {
    // ★ 국내/해외/암호화폐 세 화면이 전부 같은 형태(매수가·매수수량·
    // 현재가·수익률·수익금액·매매불가 배지)를 쓰므로 공통 헬퍼로 뺀다.
    const fmtPrice = priceUnit === "usd" ? (v) => v != null ? _usdWithKrw(v) : "-" : (v) => v != null ? won(v) : "-";
    const gridBox = el("div");
    new DataGrid(gridBox, {
      columns: [
        { key: "label", label: "종목" },
        { key: "quantity", label: "매수수량", numeric: true },
        { key: "avg_purchase_price", label: "매수가", fmt: fmtPrice, numeric: true },
        { key: "last_price", label: "현재가", fmt: fmtPrice, numeric: true },
        { key: "profit_loss_amount", label: "수익금액", fmt: (v) => v == null ? "-" : priceUnit === "usd" ? _usdSignedWithKrw(v) : signed(v, "won"), numeric: true, tone: "pnl" },
        { key: "profit_loss_rate", label: "수익률", fmt: (v) => v != null ? pct(v, 2) : "-", numeric: true, tone: "pnl" },
        { key: "tradable", label: "이 프로그램에서 매매" },
      ],
      rows: rows.map((r) => ({
        label: `${r.name || r.symbol} (${r.symbol})`,
        quantity: r.quantity, avg_purchase_price: r.avg_purchase_price,
        last_price: r.last_price, profit_loss_amount: r.profit_loss_amount, profit_loss_rate: r.profit_loss_rate,
        tradable: "🚫 불가(기존 보유)",
      })),
      page: 20, filters: false, storageKey: "grid.account.holdings." + priceUnit,
    });
    return gridBox;
  }

  function renderAccountHoldings(setup) {
    // ★★★ "계좌 현황은 보기를 눌렀을 때 확인하도록 해달라" - 준비·연결
    // 화면을 열 때마다 자동으로 계좌를 조회하지 않고, 사용자가 명시적으로
    // "보기" 버튼을 눌러야만 실제 API 호출이 일어난다.
    const wrap = el("div");
    wrap.appendChild(_pcIconEl("h2", "wallet", "내 계좌 현황(토스, 조회 전용)"));

    if (!setup.has_keys) {
      wrap.appendChild(el("div", { class: "hint", text: "토스 API 키를 등록하면 실제 보유 종목·현금 잔고를 여기서 볼 수 있습니다." }));
      return wrap;
    }

    const body = el("div");
    wrap.appendChild(body);

    function load() {
      fill(body, el("div", { class: "hint", text: "불러오는 중…" }));
      api("/api/account/holdings").then((data) => {
        body.innerHTML = "";
        // ★ 핵심만 한 줄로 두고 부연은 아이콘으로 - 스크롤을 줄인다.
        const w종목 = el("div", { class: "banner warn" });
        w종목.appendChild(_pcIconEl("span", "alert", "조회 전용 - 이 프로그램이 매매하지 않는 종목입니다"));
        w종목.appendChild(infoIcon(
          "아래 목록은 실제 계좌의 보유 현황을 그대로 보여줄 뿐입니다.\n\n"
          + "이 프로그램은 자기가 직접 산 것만 팝니다 - 여기 있는 종목이 이 엔진이 산 것이 "
          + "아니면 절대 매매하지 않습니다(안전 원칙)."));
        body.appendChild(w종목);
        body.appendChild(el("div", { html: `<b>현금 잔고</b> ${data.cash_krw != null ? won(data.cash_krw) : "-"}` }));

        const domesticRows = data.domestic_rows || [];
        const overseasRows = data.overseas_rows || [];

        body.appendChild(el("h2", { text: `🇰🇷 국내 계좌 (${domesticRows.length}종목)` }));
        if (data.domestic_profit_total != null) {
          body.appendChild(el("div", { class: dir(data.domestic_profit_total), html: `<b>수익금액 합계: ${signed(data.domestic_profit_total, "won")}</b>` }));
        }
        if (!domesticRows.length) {
          body.appendChild(el("div", { class: "hint", text: "보유 중인 국내 종목이 없습니다." }));
        } else {
          body.appendChild(_accountHoldingsTable(domesticRows, "won"));
        }

        body.appendChild(el("h2", { text: `🌍 해외 계좌 (${overseasRows.length}종목)` }));
        if (data.overseas_profit_total != null) {
          // ★ _usdSignedWithKrw 는 <br><span> 을 내는 HTML 버전 - esc() 로 감싸면 태그가
          // 그대로 글자로 보인다(숫자만 다루는 내부 계산값이라 이스케이프가 필요 없다).
          body.appendChild(el("div", { class: dir(data.overseas_profit_total), html: `<b>수익금액 합계: ${_usdSignedWithKrw(data.overseas_profit_total)}</b>` }));
        }
        if (!overseasRows.length) {
          body.appendChild(el("div", { class: "hint", text: "보유 중인 해외 종목이 없습니다." }));
        } else {
          body.appendChild(_accountHoldingsTable(overseasRows, "usd"));
        }
        body.appendChild(el("button", { class: "b ghost small ", text: "↻ 다시 조회", onclick: load }));
      }).catch((e) => {
        body.innerHTML = "";
        body.appendChild(el("div", { class: "hint fall", text: "계좌 조회 실패: " + e.message }));
        body.appendChild(el("button", { class: "b ghost small", text: "다시 시도", onclick: load }));
      });
    }

    body.appendChild(_pcIconEl("button", "eye", "계좌 현황 보기", { class: "b", onclick: load }));
    return wrap;
  }

  function renderCryptoAccountHoldings(integrations) {
    // ★★★ 토스와 마찬가지로 "보기" 버튼을 눌러야만 조회한다.
    const wrap = el("div");
    wrap.appendChild(_pcIconEl("h2", "wallet", "내 암호화폐 계좌 현황(빗썸, 조회 전용)"));

    const bithumbConfigured = integrations && integrations.bithumb && integrations.bithumb.configured;
    if (!bithumbConfigured) {
      wrap.appendChild(el("div", { class: "hint", text: "빗썸 API 키를 등록하면 실제 보유 코인·현금(KRW) 잔고를 여기서 볼 수 있습니다." }));
      return wrap;
    }

    const body = el("div");
    wrap.appendChild(body);

    function load() {
      fill(body, el("div", { class: "hint", text: "불러오는 중…" }));
      api("/api/account/crypto-holdings").then((data) => {
        body.innerHTML = "";
        // ★ 핵심만 한 줄로 두고 부연은 아이콘으로 - 스크롤을 줄인다.
        const w코인 = el("div", { class: "banner warn" });
        w코인.appendChild(_pcIconEl("span", "alert", "조회 전용 - 이 프로그램이 매매하지 않는 코인입니다"));
        w코인.appendChild(infoIcon(
          "아래 목록은 실제 계좌의 보유 현황을 그대로 보여줄 뿐입니다.\n\n"
          + "이 프로그램은 자기가 직접 산 것만 팝니다 - 여기 있는 코인이 이 엔진이 산 것이 "
          + "아니면 절대 매매하지 않습니다(안전 원칙)."));
        body.appendChild(w코인);
        body.appendChild(el("div", { html: `<b>현금(KRW) 잔고</b> ${data.cash_krw != null ? won(data.cash_krw) : "-"}` }));
        if (data.profit_total != null) {
          body.appendChild(el("div", { class: dir(data.profit_total), html: `<b>수익금액 합계: ${signed(data.profit_total, "won")}</b>` }));
        }
        if (!data.rows.length) {
          body.appendChild(el("div", { class: "hint", text: "보유 중인 코인이 없습니다." }));
        } else {
          body.appendChild(_accountHoldingsTable(data.rows, "won"));
        }
        body.appendChild(el("button", { class: "b ghost small", text: "↻ 다시 조회", onclick: load }));
      }).catch((e) => {
        body.innerHTML = "";
        body.appendChild(el("div", { class: "hint fall", text: "계좌 조회 실패: " + e.message }));
        body.appendChild(el("button", { class: "b ghost small", text: "다시 시도", onclick: load }));
      });
    }

    body.appendChild(_pcIconEl("button", "eye", "암호화폐 계좌 현황 보기", { class: "b", onclick: load }));
    return wrap;
  }

  // ★★★ "토스 API 를 등록하고 연결되면 자동으로 시세를 가져오고 메시지도
  // 바뀌어야 한다" - 키 저장 직후 이 함수를 다시 불러 화면을 통째로
  // 새로 그린다. 이름 있는 함수로 빼 둔 이유가 그것이다(showTab 은 이미
  // 활성화된 탭이면 갱신을 건너뛸 수 있어 확실하지 않다).
  async function _reloadSetupPanel() {
      const panel = $('.panel[data-panel="setup"]');
      panel.innerHTML = "";
      panel.appendChild(skeleton(80));
      // ★★★ "준비·연결을 열면 오래 걸린다" - 예전엔 네트워크 진단(외부 4곳 접속)과 실거래
      // 사전 점검(증권사 API 호출, ~1.7초)까지 전부 끝나야 화면이 그려졌다. 둘 다 접힌
      // "자세히" 항목이라, 바로 필요한 setup·integrations 만 기다려 먼저 그리고
      // 느린 둘은 뒤에서 받아 채운다(개요 배지도 도착하는 대로 갱신).
      let setup, integrations;
      try {
        [setup, integrations] = await Promise.all([api("/api/setup"), api("/api/integrations")]);
      } catch (e) {
        panel.innerHTML = "";
        toast(e.message, "error");
        return;
      }
      panel.innerHTML = "";
      const state = { diag: null, pf: null };
      // ★ 탭을 오가도 백그라운드로 채워지는 값이 유지되도록, 각 탭의 내용은 한 번만 만들어
      //   두고 탭을 바꿀 때 다시 끼워 넣는다(진단·사전 점검은 도착하는 대로 채워진다).
      const overviewHolder = el("div", {}, [renderSetupOverview(setup, integrations, null, null)]);
      const refreshOverview = () => {
        overviewHolder.replaceChildren(renderSetupOverview(setup, integrations, state.diag, state.pf));
      };
      const tabOverview = el("div", {}, [overviewHolder, renderSetupStatus(setup)]);
      const tabAccount = el("div", {}, [renderAccountHoldings(setup), renderCryptoAccountHoldings(integrations)]);
      // ★★★ 실제로 겪은 문제 - 키를 저장해도 화면이 그대로라 "미연계" 표시와 "시세를 못 가져온다"는
      // 메시지가 계속 남아 있었다. onSaved 콜백으로 저장 직후 이 화면을 통째로 다시 그린다.
      const tabKeys = el("div", {}, [renderCredentialsForm(() => _reloadSetupPanel(), integrations)]);

      // ★ 아래 둘은 위 연계 테스트와 겹치는 항목이 있다 - 다른 점은 "지금 이 순간"(오늘 개장일인지,
      // 지금이 매매시간대인지, 네트워크가 실제로 뚫려있는지) 기준이라는 것이다.
      const connBody = el("div", {}, [el("div", { class: "hint", text: "확인 중…" })]);
      const pfBody = el("div", {}, [el("div", { class: "hint", text: "확인 중…" })]);
      const connTitle = _pcIconEl("h2", "wifi", "네트워크 연결 진단");
      connTitle.appendChild(infoIcon(
        "위 연계 테스트가 'API 키가 맞는지'를 본다면, 이건 '인터넷 자체가 막혀있지 않은지'를 봅니다.\n\n"
        + "회사·학교 네트워크나 백신·방화벽이 막고 있으면 여기서 드러납니다."));
      const tabDiag = el("div", {}, [connTitle, connBody, _pcIconEl("h2", "shield", "실거래 사전 점검"), pfBody]);

      renderTabs(panel, "setup", [
        { id: "overview", label: "한눈에", build: () => tabOverview },
        { id: "keys", label: "외부 연동", build: () => tabKeys },
        { id: "account", label: "계좌 현황", build: () => tabAccount },
        { id: "diag", label: "진단·점검", build: () => tabDiag },
      ]);

      const failNote = (box, e) => { box.innerHTML = ""; box.appendChild(el("div", { class: "hint fall", text: "불러오지 못했습니다: " + e.message })); };
      api("/api/net/diagnose").then((diag) => {
        state.diag = diag;
        connBody.innerHTML = "";
        connBody.appendChild(renderConnCheck(diag));
        refreshOverview();
      }).catch((e) => failNote(connBody, e));
      api("/api/preflight").then((pf) => {
        state.pf = pf;
        pfBody.innerHTML = "";
        pfBody.appendChild(renderPreflight(pf));
        refreshOverview();
      }).catch((e) => failNote(pfBody, e));
  }

  registerPanel("setup", { onShow: _reloadSetupPanel });

  // ━━ 테마 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  // ★ 테마·감시목록 한 줄을 코드가 아니라 "이름(코드)" 배지 한 칸으로 압축해서 보여준다
  // (예전엔 <ul><li> 로 한 종목씩 줄바꿈해 종목 많은 테마는 한참 스크롤해야 했다).
  function _themeChips(codes, names) {
    const wrap = el("div", { style: { display: "flex", flexWrap: "wrap", gap: "6px" } });
    codes.forEach((c) => {
      const label = names && names[c] ? `${names[c]} (${c})` : c;
      wrap.appendChild(el("span", { class: "badge tech", style: { cursor: "default" }, text: label }));
    });
    return wrap;
  }

  // ★★★ "심플&모던, 스크롤 많은 네비게이션 지양" - 테마가 많아지면 접힌 항목을 하나하나
  // 열어 찾아야 했다. 검색창으로 테마명·종목명·코드를 걸러 안 맞는 항목은 감춘다.
  function _themeFilterRow(onFilter) {
    const wrap = el("div", { class: "field", style: { maxWidth: "360px" } });
    const input = el("input", { type: "text", placeholder: "테마명·종목명·코드로 검색" });
    input.addEventListener("input", () => onFilter(input.value.trim().toLowerCase()));
    wrap.appendChild(_pcIconEl("label", "search", "검색"));
    wrap.appendChild(input);
    return wrap;
  }

  function renderThemesPanel(data) {
    const panel = $('.panel[data-panel="themes"]');
    panel.innerHTML = "";
    panel.appendChild(el("div", {
      class: "banner",
      text: "종목을 고르는 화면이 아니라 자동 선정이 뒤질 범위를 정하는 화면입니다.",
    }));

    const themes = data.themes || {};
    const names = data.names || {};
    const groups = []; // ★ 검색 필터가 켜고 끌 { el, haystack } 들을 모아 둔다.

    const listWrap = el("div", { class: "scroll-box" });
    panel.appendChild(_themeFilterRow((q) => {
      groups.forEach((g) => { g.el.style.display = !q || g.haystack.includes(q) ? "" : "none"; });
    }));
    panel.appendChild(listWrap);

    // ★ 종목코드만 나오던 문제 - 옆에 이름을 함께 보여준다(themes.yaml의
    // 인라인 주석에서 뽑아온 이름, 없으면 코드 그대로). 국내 테마는 기본으로
    // 접어 두고(테마 수가 많을 수 있어서), 나머지 두 목록만 펼쳐 둔다.
    Object.entries(themes).forEach(([name, codes]) => {
      const details = el("details", { class: "acc" });
      const summary = el("summary", { text: name });
      summary.appendChild(el("span", { class: "acc-meta", text: `${codes.length}종목` }));
      details.appendChild(summary);
      const body = el("div", { class: "acc-body" }, [_themeChips(codes, names)]);
      details.appendChild(body);
      listWrap.appendChild(details);
      const haystack = (name + " " + codes.map((c) => `${names[c] || ""} ${c}`).join(" ")).toLowerCase();
      groups.push({ el: details, haystack });
    });

    // ★★ "해외주식·암호화폐 종목도 여기서 보여야 한다"는 요청 - 테마
    // 스크리닝 개념 자체는 국내주식 전용이라 이 두 시장은 [설정] 의
    // 관심종목(watchlist)을 그대로 읽기 전용으로 보여준다. 실제 추가·
    // 수정은 설정 화면에서 한다 - 여기서 또 만들면 두 군데를 손봐야 하는
    // 창구가 생긴다.
    const crypto = data.crypto_watchlist || [];
    const cryptoDetails = el("details", { class: "acc", open: true });
    const cryptoSummary = _pcIconEl("summary", "wallet", "암호화폐 감시 목록");
    cryptoSummary.appendChild(el("span", { class: "acc-meta", text: `${crypto.length}종목` }));
    cryptoDetails.appendChild(cryptoSummary);
    const cryptoBody = el("div", { class: "acc-body" });
    if (!crypto.length) {
      cryptoBody.appendChild(el("div", { class: "hint", text: "등록된 종목이 없습니다." }));
    } else {
      cryptoBody.appendChild(_themeChips(crypto));
    }
    cryptoBody.appendChild(el("div", { class: "hint", text: "추가·수정은 [설정] → 암호화폐 섹션에서 합니다." }));
    cryptoDetails.appendChild(cryptoBody);
    listWrap.appendChild(cryptoDetails);
    groups.push({ el: cryptoDetails, haystack: ("암호화폐 감시 목록 " + crypto.join(" ")).toLowerCase() });

    const overseas = data.overseas_watchlist || [];
    const overseasDetails = el("details", { class: "acc", open: true });
    const overseasSummary = _pcIconEl("summary", "globe", "해외주식 관심 종목");
    overseasSummary.appendChild(el("span", { class: "acc-meta", text: `${overseas.length}종목` }));
    overseasDetails.appendChild(overseasSummary);
    const overseasBody = el("div", { class: "acc-body" });
    if (!overseas.length) {
      overseasBody.appendChild(el("div", { class: "hint", text: "등록된 종목이 없습니다." }));
    } else {
      overseasBody.appendChild(_themeChips(overseas));
    }
    overseasBody.appendChild(el("div", { class: "hint", text: "추가·수정은 [설정] → 해외주식 섹션에서 합니다." }));
    overseasDetails.appendChild(overseasBody);
    listWrap.appendChild(overseasDetails);
    groups.push({ el: overseasDetails, haystack: ("해외주식 관심 종목 " + overseas.join(" ")).toLowerCase() });

    const addForm = el("div", { class: "card" });
    addForm.appendChild(el("div", { html: "<b>국내주식 테마에 종목 추가</b>" }));
    const themeInput = el("input", { type: "text", placeholder: "테마명(예: 로봇)" });
    const symInput = el("input", { type: "text", placeholder: "종목코드 6자리" });
    addForm.appendChild(themeInput);
    addForm.appendChild(symInput);
    addForm.appendChild(el("button", {
      class: "b", text: "추가",
      onclick: async () => {
        try {
          await api("/api/themes", { method: "POST", body: { theme: themeInput.value.trim(), symbol: symInput.value.trim() } });
          toast("추가했습니다.");
          renderThemesPanel(await api("/api/themes"));
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }));
    panel.appendChild(addForm);
  }

  registerPanel("themes", {
    onShow: async () => {
      try {
        renderThemesPanel(await api("/api/themes"));
      } catch (e) {
        toast(e.message, "error");
      }
    },
  });

  // ━━ 설정 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★★ JSON·YAML 을 그대로 보여주지 않는다. CONFIG_SCHEMA 를 순회해
  // 섹션마다 <section>, 항목마다 buildField() 를 그린다.

  let _configRaw = null;

  let _activeConfigGroup = null;  // ★ 모듈 스코프 - 저장 후 다시 그려도 보고 있던 탭이 안 바뀌게 유지한다.
  const _cfgActiveSection = {};  // ★ 그룹명 -> 그 그룹 안에서 지금 보고 있는 섹션 제목(모듈 스코프, 위와 같은 이유).

  function _cfgIsWide() {
    return typeof window.matchMedia === "function" && window.matchMedia("(min-width:1024px)").matches;
  }

  // ★★★ "심플&모던, 스크롤 많은 네비게이션 지양" - 국내주식 그룹만 섹션(카드)이 7개라
  // 예전엔 전부 펼쳐 쌓아 두고 칩으로 그 위치까지 스크롤만 시켜줬다(quicknav) - 카드
  // 자체는 다 떠 있어서 스크롤 총량은 그대로였다. 이제 섹션 목록(데스크톱은 왼쪽 세로,
  // 모바일은 위쪽 칩 줄) + 오른쪽/아래 폼 한 칸으로 나눠, 한 번에 섹션 하나만 그린다.
  // 섹션이 하나뿐인 그룹(암호화폐·해외주식·스윙)은 목록 없이 바로 그 섹션만 보여준다.
  function _cfgSectionShell(groupName, built) {
    if (built.length <= 1) {
      const only = el("div");
      built.forEach(({ sectionEl }) => only.appendChild(sectionEl));
      return only;
    }
    if (!_cfgActiveSection[groupName] || !built.some(({ section }) => section.title === _cfgActiveSection[groupName])) {
      _cfgActiveSection[groupName] = built[0].section.title;
    }
    const wide = _cfgIsWide();
    const nav = el("div", {
      class: "chips",
      style: wide ? { flexDirection: "column", alignItems: "stretch", minWidth: "180px", flex: "0 0 auto" } : {},
    });
    const pane = el("div", { style: { flex: "1", minWidth: "0" } });
    // ★★★ 섹션을 전부 pane 에 심어 두고 display 로만 켜고 끈다(그룹 탭과 같은 패턴) -
    // 안 보이는 섹션을 통째로 지웠다(innerHTML="") 되살리면, 그 사이 입력해 둔 값이
    // 저장(collectConfig 는 DOM 에서 값을 읽는다) 시점에 화면에 없다는 이유로 조용히
    // 원래 값으로 되돌아간다 - 실제로 겪을 뻔한 데이터 유실이라 반드시 display 로만 전환한다.
    function showSection(title) {
      _cfgActiveSection[groupName] = title;
      built.forEach(({ section, sectionEl }) => { sectionEl.style.display = section.title === title ? "" : "none"; });
      $$(".chip", nav).forEach((c) => c.classList.toggle("active", c.dataset.sectionTitle === title));
    }
    built.forEach(({ section, sectionEl }) => {
      pane.appendChild(sectionEl);
      const chip = el("button", {
        type: "button", text: section.title, "data-section-title": section.title,
        class: "chip" + (section.title === _cfgActiveSection[groupName] ? " active" : ""),
        onclick: () => showSection(section.title),
      });
      nav.appendChild(chip);
    });
    showSection(_cfgActiveSection[groupName]);
    const shell = el("div", { style: { display: "flex", gap: "var(--s3)", alignItems: "flex-start", flexDirection: wide ? "row" : "column" } });
    shell.appendChild(nav);
    shell.appendChild(pane);
    return shell;
  }

  function renderConfig(data) {
    const panel = $('.panel[data-panel="config"]');
    panel.innerHTML = "";
    _configRaw = data.raw;

    // "왕복 비용의 2배 미만이면 저장이 거부됩니다"를 미리 알려준다 -
    // 거부당한 뒤에야 이유를 찾게 하면 안 된다.
    panel.appendChild(el("div", {
      class: "banner",
      text: `왕복 비용 ${pct(data.breakeven_pct, 3)}(본전 기준) - 익절폭이 이 값의 2배 미만이면 저장이 거부됩니다.`,
    }));
    // 입력 분류 안내: 무엇을 꼭 넣어야 하고 무엇이 자동인지 한눈에.
    const guide = el("div", { class: "banner" });
    guide.appendChild(el("span", { html: `<span class="tier-badge required">필수</span> 반드시 정해야 하는 값(시장 사용·모드·투자금액·동시 보유·매매 속도) · `
      + `<span class="tier-badge optional">선택</span> 넣으면 더 좋은 값(관심 종목) · <span class="tier-badge auto">자동</span> 프로그램이 정한 값("자동 설정"에 모아 둠, 필요 없으면 접을 수 있음)` }));
    panel.appendChild(guide);
    if (data.raw && (data.raw.style === "fast" || data.raw.style === "scalp")) {
      panel.appendChild(el("div", { class: "banner warn",
        text: data.raw.style === "scalp"
          ? "⚡ 스캘핑(실험용) 모드: 시세 확인 주기·손절/익절 폭·최대 보유 시간·거래 횟수·진입 기법이 자동으로 정해져 아래 자동 설정의 같은 항목 대신 적용됩니다. 시뮬레이션에서는 크게 손실이 났으니 모의매매로만 확인하세요."
          : "⚡ 빠른 단타 모드: 시세 확인 주기·재진입 금지·일일 거래 한도·진입 기법·익절폭(4%→3.2%)이 자동으로 정해져 아래 자동 설정의 같은 항목 대신 적용됩니다(손절폭은 보통과 동일 2.5%). 보통으로 바꾸면 아래 값이 그대로 쓰입니다." }));
    }

    // ★★★ "국내주식 거래 중일 때는 국내 관련 설정만 잠기고, 암호화폐·
    // 해외주식 설정은 그대로 할 수 있어야 한다" - 시장별로 따로 안내한다.
    const lockedMarkets = [];
    if (data.locked && data.locked.domestic) lockedMarkets.push("국내주식(위험관리·매매기법 포함)");
    if (data.locked && data.locked.crypto) lockedMarkets.push("암호화폐");
    if (data.locked && data.locked.overseas) lockedMarkets.push("해외주식");
    if (data.locked && data.locked.swing) lockedMarkets.push("스윙");
    if (lockedMarkets.length) {
      panel.appendChild(el("div", {
        class: "banner warn",
        text: `${lockedMarkets.join(", ")} 거래 중에는 해당 설정을 바꿀 수 없습니다. 다른 시장 설정은 그대로 저장할 수 있습니다.`,
      }));
    }

    const techs = {};
    (data.techniques || []).forEach((t) => {
      // ★ techs.entry/exit 는 국내주식 전용, techs.crypto_entry/crypto_exit 는 암호화폐 전용,
      // techs.swing_entry/swing_exit 는 스윙 전용으로 명확히 나눈다 - 섞이면 국내주식
      // 체크리스트에 다른 시장 기법이 끼어드는 버그가 된다.
      const key = t.market === "crypto" ? "crypto_" + t.phase : t.market === "swing" ? "swing_" + t.phase : t.phase;
      (techs[key] || (techs[key] = [])).push(t);
    });

    const form = el("form", { id: "config-form", onsubmit: (e) => e.preventDefault() });

    // ★★★ "거래선택 / 공통 / 국내주식 / 해외주식 / 암호화폐"로 최상위
    // 그룹 자체를 나눈다 - 이전엔 "매매 기본" 그룹 안에서 시장 탭으로
    // 전환하는 방식이었는데, 그보다 그룹 자체를 시장별로 쪼개는 게 더
    // 명확하다는 지적에 따라 바꿨다.
    const GROUP_ORDER = ["거래선택", "공통", "국내주식", "해외주식", "암호화폐", "스윙", "정보·알림", "시스템"];
    const GROUP_ICON = {
      "거래선택": "target", "공통": "sliders", "국내주식": "chart", "해외주식": "globe",
      "암호화폐": "wallet", "스윙": "trending-up", "정보·알림": "bell", "시스템": "layers",
    };
    // ★★★ "설정에서 거래선택시 현재 매매중이 아닌 건 선택할 수 있어야 한다" - 예전엔
    // 그룹(탭) 전체를 하나의 시장에 묶어 잠갔다("거래선택" 탭 전체를 국내주식 거래 중이면
    // 통째로 잠금). 그런데 "거래할 시장"·"투자금액" 섹션에는 crypto.enabled·overseas.budget_usd
    // 처럼 서로 다른 시장 설정이 한 카드에 섞여 있어서, 국내주식만 거래 중이어도 아직 켜지도
    // 않은 스윙·해외주식·암호화폐 사용 여부까지 못 바꾸는 버그가 됐다. 이제 필드 하나하나의
    // 최상위 키(예: "crypto.enabled" → "crypto")로 잠금을 판정한다 - 백엔드 검증
    // (_DOMESTIC_CONFIG_KEYS 등, server.py)과 정확히 같은 기준이라 "화면은 잠겨 보이는데
    // 저장은 되는" 또는 그 반대의 불일치가 없다.
    const KEY_MARKET = {
      mode: "domestic", capital: "domestic", risk: "domestic", screen: "domestic",
      entry: "domestic", exit: "domestic", strategy: "domestic", costs: "domestic",
      live: "domestic", account_seq: "domestic",
      crypto: "crypto", overseas: "overseas", swing: "swing",
      sizing: "all", style: "all",  // ★ 세 시장이 공유하는 값 - 아무 시장이나 거래 중이면 잠긴다.
    };
    const MARKET_LABEL = { domestic: "국내주식", crypto: "암호화폐", overseas: "해외주식", swing: "스윙" };

    function _fieldLockMarket(path) {
      return KEY_MARKET[(path || "").split(".")[0]] || null;
    }

    function _lockedMarketOf(fieldMarket) {
      if (!fieldMarket || !data.locked) return null;
      if (fieldMarket === "all") {
        return Object.keys(data.locked).find((m) => data.locked[m]) || null;
      }
      return data.locked[fieldMarket] ? fieldMarket : null;
    }

    const byGroup = {};
    CONFIG_SCHEMA.forEach((section) => {
      const g = section.group || "기타";
      (byGroup[g] || (byGroup[g] = [])).push(section);
    });

    function renderSectionCard(section) {
      const sectionEl = el("section", { class: "card" });
      // ★★★ 섹션 설명(note)도 제목 호버로 - 섹션마다 한 줄씩 깔리면
      // 설정 화면이 그만큼 길어진다. 제목에 달아 두면 필요할 때만 본다.
      sectionEl.appendChild(section.note
        ? titleWithHelp(section.title, section.note)
        : el("h2", { text: section.title }));
      // ★★★ "자동으로 정해지는 것과 사용자가 반드시 입력해야 하는 것을 분류" - 필수·선택 항목은 그대로 보이고,
      // 프로그램이 기본값으로 정하는 자동 항목은 접힌 영역에 모아 둔다(필요할 때만 열어 수정).
      // ★ 이 섹션 안에서 실제로 잠긴 필드가 있으면 어느 시장 때문인지 모아 뒀다가
      // 카드 하단에 한 번만 안내한다(필드마다 반복해서 적지 않는다).
      const lockedMarketsInSection = new Set();

      function _applyFieldLock(node, f) {
        const locked = _lockedMarketOf(_fieldLockMarket(f.path));
        if (!locked) return;
        lockedMarketsInSection.add(locked);
        node.style.opacity = "0.55";
        $$("input, select, textarea, button", node).forEach((el2) => { el2.disabled = true; });
      }

      const autoFields = [];
      section.fields.forEach((f) => {
        const tier = fieldTier(f.path);
        if (tier === "auto") { autoFields.push(f); return; }
        const node = buildField(f, data.raw, techs);
        const lab = node.querySelector("label");
        if (lab) lab.insertAdjacentElement("afterbegin", el("span", { class: "tier-badge " + tier, text: tier === "required" ? "필수" : "선택" }));
        _applyFieldLock(node, f);
        sectionEl.appendChild(node);
      });
      if (autoFields.length) {
        // ★★★ "항목별로 클릭해야 세부 내용을 볼 수 있는데, 스크롤하지 않고 쉽게 확인할 수
        // 있게" 요청 - 예전엔 접어 뒀다가 눌러야 자동 설정이 펼쳐졌는데, 펼칠 때마다 페이지가
        // 늘어나 그 아래 섹션을 찾으려면 다시 스크롤해야 했다. 위에 추가한 섹션 이동 칩(quicknav)
        // 덕분에 섹션까지는 스크롤 없이 바로 갈 수 있게 됐으니, 이제 그 안의 내용도 클릭 없이
        // 바로 보이게 기본으로 펼쳐 둔다(그래도 필요 없으면 접을 수 있게 <details> 는 유지).
        const det = el("details", { class: "auto-fields", open: true });
        det.appendChild(el("summary", { text: `자동 설정 ${autoFields.length}개 - 프로그램이 정한 값(기본값 권장, 필요할 때만 수정)` }));
        autoFields.forEach((f) => {
          const node = buildField(f, data.raw, techs);
          const lab = node.querySelector("label");
          if (lab) lab.insertAdjacentElement("afterbegin", el("span", { class: "tier-badge auto", text: "자동" }));
          _applyFieldLock(node, f);
          det.appendChild(node);
        });
        sectionEl.appendChild(det);
      }

      // ★★★ "설정에서 거래선택시 현재 매매중이 아닌 건 선택할 수 있어야 한다" - 이제
      // 필드 하나하나를 그 필드가 속한 시장이 거래 중일 때만 잠근다(위 KEY_MARKET 참고).
      // 그래서 예를 들어 국내주식만 거래 중이어도 아직 안 켠 스윙·해외주식·암호화폐 사용
      // 여부·투자금액은 그대로 바꿀 수 있다 - 실제로 지금 거래 중인 시장의 필드만 잠긴다.
      if (lockedMarketsInSection.size) {
        const names = Array.from(lockedMarketsInSection).map((m) => MARKET_LABEL[m] || m).join(", ");
        sectionEl.appendChild(el("div", {
          class: "hint fall",
          text: `${names} 거래 중에는 그 시장에 해당하는 항목을 바꿀 수 없습니다(같은 카드의 다른 시장 항목은 그대로 바꿀 수 있습니다).`,
        }));
      }
      return sectionEl;
    }

    // ★★★ 가로 탭 + 고정 프레임 구조로 바꾼다 - 탭(거래선택/공통/국내주식/
    // 해외주식/암호화폐/정보·알림/시스템)은 위에 고정, 그 안 내용만 스크롤,
    // 저장·되돌리기 버튼은 화면 하단에 고정한다.
    //
    // ★★ 탭을 누를 때마다 이전 탭의 DOM을 지우고 다시 그리면, 아직 저장
    // 안 한 채로 탭을 바꾼 편집 내용이 조용히 사라질 위험이 있다(다른
    // 탭에는 입력값을 어디에도 보관 안 해두므로). 그래서 모든 그룹의
    // 필드를 한꺼번에 DOM에 만들어 두고, display 로만 켜고 끈다 - 대시보드
    // 섹션을 이렇게 만들어 안전했던 것과 같은 패턴이다.
    const configGroups = GROUP_ORDER.filter((g) => byGroup[g]);
    // ★★★ "저장을 누르면 거래선택 탭으로 이동하는 문제" - 예전엔 이 값이
    // renderConfig() 안의 지역 변수라서, 저장 후 onShowConfig() 가
    // renderConfig() 를 다시 부를 때마다 항상 configGroups[0](거래선택)
    // 으로 리셋됐다. 모듈 스코프 값이 없을 때(맨 처음 화면을 열 때)만
    // 기본값을 주고, 그 외엔 마지막으로 보고 있던 탭을 그대로 쓴다.
    if (_activeConfigGroup == null || !configGroups.includes(_activeConfigGroup)) {
      _activeConfigGroup = configGroups[0];
    }

    const tabSeg = el("div", { class: "seg", style: { flexWrap: "wrap" } });
    const groupPanels = {};
    configGroups.forEach((groupName) => {
      const btn = _pcIconEl("button", GROUP_ICON[groupName], groupName, {
        class: groupName === _activeConfigGroup ? "active" : "",
      });
      btn.addEventListener("click", () => {
        _activeConfigGroup = groupName;
        $$("button", tabSeg).forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        Object.entries(groupPanels).forEach(([g, node]) => {
          node.style.display = g === groupName ? "" : "none";
        });
      });
      tabSeg.appendChild(btn);
    });

    // ★ 그룹(시장) 하나를 고르면 그 안의 섹션은 _cfgSectionShell() 이 목록 + 폼 한 칸으로
    // 나눠 보여준다(위 주석 참고) - 예전처럼 섹션 카드를 전부 쌓아 두지 않는다.
    const scrollArea = el("div", { class: "config-scroll" });
    configGroups.forEach((groupName) => {
      const sections = byGroup[groupName];
      const groupPanel = el("div", { style: { display: groupName === _activeConfigGroup ? "" : "none" } });
      const built = sections.map((section) => ({ section, sectionEl: renderSectionCard(section) }));
      groupPanel.appendChild(_cfgSectionShell(groupName, built));
      groupPanels[groupName] = groupPanel;
      scrollArea.appendChild(groupPanel);
    });

    form.classList.add("config-frame");
    form.appendChild(tabSeg);
    form.appendChild(scrollArea);

    const DOMESTIC_TOP_KEYS = ["mode", "capital", "risk", "screen", "entry", "exit", "strategy", "costs", "live", "account_seq"];
    const actions = el("div", { class: "config-actions" });
    actions.appendChild(el("button", {
      // ★★★ "국내주식 거래중일 때는 국내 관련 설정에서만 저장이 막혀야
      // 한다" - 저장 버튼 자체는 항상 눌러지게 두고(다른 시장 설정은
      // 저장할 수 있어야 하니), 잠긴 시장에 속한 최상위 키만 전송에서
      // 뺀다. 실제로 뭔가를 바꿨는데 전부 잠긴 시장 값뿐이면 서버가
      // 그 사실을 알려준다(post_config 의 시장별 거부 로직).
      class: "b", type: "button", text: "저장",
      onclick: async () => {
        try {
          const newRaw = collectConfig(form, _configRaw);
          const locks = data.locked || {};
          // ★★★ 실제로 겪은 문제 - 거래 중인 시장의 설정 키를 여기서
          // 조용히 지워 보내고도 항상 "저장했습니다"만 띄웠다. 사용자는
          // 예를 들어 국내주식 거래 중에 종목선정 기준(최소 거래대금 등)을
          // 바꾸고 저장을 눌러도 성공 토스트만 보고 "반영됐다"고 믿게
          // 되는데, 실제로는 그 부분이 통째로 안 보내져 저장 자체가 안
          // 됐다. 지우기 전에 실제로 뭔가 바뀌었는지 비교해서, 바뀐 게
          // 있으면 어느 시장 때문에 저장되지 못했는지 분명히 알린다.
          const droppedMarkets = [];
          const dropIfLocked = (locked, keys, label) => {
            if (!locked) return;
            const changed = keys.some((k) => JSON.stringify(newRaw[k]) !== JSON.stringify(_configRaw[k]));
            if (changed) droppedMarkets.push(label);
            keys.forEach((k) => { delete newRaw[k]; });
          };
          dropIfLocked(locks.domestic, DOMESTIC_TOP_KEYS, "국내주식(위험관리·매매기법·종목선정 포함)");
          dropIfLocked(locks.crypto, ["crypto"], "암호화폐");
          dropIfLocked(locks.overseas, ["overseas"], "해외주식");
          // ★★★ 실제로 겪은 문제(2026-09-24) - 위 세 시장만 지우고 "swing"
          // 은 빼먹었다. newRaw 는 항상 raw 설정 전체를 통째로 복제한 것이라
          // (collectConfig 참고) 스윙을 전혀 안 건드려도 body 에 "swing" 키가
          // 항상 들어 있다. 스윙이 거래 중이면 서버(post_config)가 이 키만
          // 보고 "스윙 거래 중" 이라며 저장 전체(예: 암호화폐만 바꿨는데도)를
          // 거부해 버렸다 - 사용자가 안 건드린 시장 때문에 저장이 막히고,
          // 심지어 메시지도 "스윙 설정이 바뀌었다"는 것처럼 혼동을 줬다.
          dropIfLocked(locks.swing, ["swing"], "스윙");
          await api("/api/config", { method: "POST", body: newRaw });
          if (droppedMarkets.length) {
            toast(
              `${droppedMarkets.join(", ")} 거래 중이라 해당 설정은 저장되지 않았습니다 - 나머지는 저장했습니다. `
              + "반영하려면 거래를 멈춘 뒤 다시 저장하세요.",
              "error",
            );
          } else {
            toast("저장했습니다.");
          }
          onShowConfig();
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }));
    actions.appendChild(el("button", { class: "b ghost", type: "button", text: "되돌리기", onclick: () => onShowConfig() }));
    form.appendChild(actions);

    panel.appendChild(form);
  }

  async function onShowConfig() {
    try {
      renderConfig(await api("/api/config"));
    } catch (e) {
      toast(e.message, "error");
    }
  }

  registerPanel("config", { onShow: onShowConfig });

  // ━━ 속보 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function _newsIcon(name) {
    return (window.UI && window.UI.icon) ? window.UI.icon(name) : "";
  }

  function renderNewsItem(it) {
    const row = el("div", { class: "card", style: { marginBottom: "var(--s2)", padding: "var(--s2) var(--s3)" } });
    const head = el("div", { style: { display: "flex", justifyContent: "space-between", gap: "var(--s2)" } });
    // ★ 링크는 외부(RSS)에서 온 값이라 javascript: 같은 주소가 섞여 있으면 눌렀을 때 스크립트가 돈다 - http(s) 만 링크로 쓴다.
    const safeLink = /^https?:\/\//i.test(String(it.link || "")) ? it.link : null;
    head.appendChild(safeLink
      ? el("a", { href: safeLink, target: "_blank", rel: "noopener noreferrer", text: it.title, style: { fontWeight: "600" } })
      : el("span", { text: it.title, style: { fontWeight: "600" } }));
    if (it.risk && it.risk.length) {
      head.appendChild(el("span", { class: "badge tech", style: { background: "var(--rise)", color: "#fff", cursor: "default" }, text: "⚠ 위험어" }));
    }
    row.appendChild(head);
    row.appendChild(el("div", { class: "hint", text: `${it.publisher || it.source || ""} · ${(it.published || "").slice(0, 16).replace("T", " ")}` }));
    const tags = [].concat(it.themes || [], it.symbols || []);
    if (tags.length) row.appendChild(el("div", { class: "hint", text: tags.join(", ") }));
    return row;
  }

  function renderNewsGroup(g) {
    // ★ 그룹마다 접이식으로 쌓지 않고 탭으로 나눈다 - 이 함수는 탭 하나의 내용만 만든다.
    const body = el("div");
    if (g.risk) body.appendChild(el("div", { class: "banner warn", text: `⚠ ${g.risk}` }));
    (g.top || []).forEach((it) => body.appendChild(renderNewsItem(it)));
    if (g.rest && g.rest.length) {
      const more = el("details", { class: "acc" });
      more.appendChild(el("summary", { text: `더보기 (${g.rest.length}건)` }));
      const moreBody = el("div", { class: "acc-body" });
      g.rest.forEach((it) => moreBody.appendChild(renderNewsItem(it)));
      more.appendChild(moreBody);
      body.appendChild(more);
    }
    return body;
  }

  const NEWS_MODE_NOTE = {
    off: "꺼짐 - 속보를 보지 않습니다.",
    view: "참고용으로만 표시합니다 (매매 판단에 개입하지 않습니다).",
    avoid: "위험 신호가 있으면 후보에서 제외합니다.",
    boost: "테마 점수에 가점으로 반영합니다 (권장하지 않습니다).",
  };

  async function renderNews() {
    const panel = $('.panel[data-panel="news"]');
    panel.innerHTML = "";
    const head = el("div", { class: "page-head" });
    head.appendChild(el("h1", { html: _newsIcon("newspaper") + " 속보" }));
    const refreshBtn = el("button", {
      class: "b ghost small", html: `${_newsIcon("refresh")} 새로고침`,
      onclick: async () => {
        try {
          await api("/api/news/refresh", { method: "POST" });
          toast("새로고침했습니다.");
          renderNews();
        } catch (e) {
          toast(e.message, "error");
        }
      },
    });
    head.appendChild(refreshBtn);
    panel.appendChild(head);
    panel.appendChild(skeleton(200));
    try {
      const data = await api("/api/news");
      panel.innerHTML = "";
      panel.appendChild(head);
      panel.appendChild(el("div", {
        class: "banner",
        text: "매매 개입 방식: " + (NEWS_MODE_NOTE[data.mode] || data.mode),
      }));
      if (!data.groups || !data.groups.length) {
        panel.appendChild(el("div", { class: "hint empty", text: "표시할 속보가 없습니다." }));
        return;
      }
      // ★★★ "스크롤 없이 첫 화면에서 가장 중요한 숫자가" - 그룹별 표(탭)를 보기 전에
      // 전체 속보·위험 신호 그룹 수를 먼저 보여준다.
      const total = data.groups.reduce((a, g) => a + (g.total || 0), 0);
      const riskGroups = data.groups.filter((g) => g.risk).length;
      const kGrid = el("div", { class: "kpi-grid" });
      const kpi = (label, value, sub, cls) => {
        const k = el("div", { class: "kpi" });
        k.appendChild(el("div", { class: "kpi-label", text: label }));
        k.appendChild(el("div", { class: "kpi-value " + (cls || ""), text: value }));
        k.appendChild(el("div", { class: "kpi-sub", text: sub || " " }));
        return k;
      };
      kGrid.appendChild(kpi("전체 속보", `${total}건`));
      kGrid.appendChild(kpi("주제", `${data.groups.length}개`));
      kGrid.appendChild(kpi("위험 신호", `${riskGroups}개 그룹`, "", riskGroups ? "fall" : ""));
      panel.appendChild(kGrid);
      renderTabs(panel, "news", data.groups.map((g) => ({
        id: g.group, label: `${g.group} (${g.total})` + (g.risk ? " ⚠" : ""), build: () => renderNewsGroup(g),
      })));
    } catch (e) {
      panel.innerHTML = "";
      panel.appendChild(head);
      panel.appendChild(el("div", { class: "banner danger", text: "속보를 불러오지 못했습니다: " + e.message }));
    }
  }

  registerPanel("news", { onShow: renderNews });

  // ━━ 시장 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  const MARKET_GROUP_DESC = {
    "국내": "국내 지수와 코스피·코스닥 상위 종목 — 오늘 우리 시장의 기본 배경입니다.",
    "선물": "미국 지수선물 — 밤사이 유일하게 움직인다. 다음 날 아침 갭의 힌트입니다.",
    "해외": "주요국 지수 — 미국·일본·중국 시장의 방향입니다.",
    "미국 대형주": "미국 시가총액 상위 종목 — 우리 밤 시간에 움직입니다.",
    "미국-HBM": "메모리 반도체(HBM·DRAM) 관련 미국 종목·ETF — 국내 반도체 테마와 가장 직접 연결됩니다.",
    "환율": "원화 환율과 달러인덱스 — 외국인 수급의 배경입니다.",
    "암호화폐": "비트코인 등 위험자산 심리의 참고 지표입니다.",
  };

  // ★★ 같은 원인(error_code)으로 여러 카드가 한꺼번에 실패하면(예: 네이버가
  // 통째로 막힘) 카드마다 "조회 실패: ..."를 반복하는 대신 배너 하나로
  // 묶는다 - suppressedCodes 에 들어있는 코드는 카드에서 메시지를 생략한다.
  function renderMarketCard(row, suppressedCodes) {
    const card = el("div", { class: "card", style: { minWidth: "196px" } });
    const titleText = row.symbol ? `${row.label} (${row.symbol})` : row.label;
    card.appendChild(el("div", { style: { fontWeight: "600" }, text: titleText }));

    if (row.error) {
      const suppressed = suppressedCodes && row.error_code && suppressedCodes.has(row.error_code);
      card.appendChild(el("div", {
        class: "hint",
        text: suppressed ? "-" : "조회 실패: " + row.error,
      }));
      return card;
    }

    if (row.sessions) {
      const s = row.sessions;
      // ★★ .card 의 기본 gap(8px)이 세션 줄 하나하나 사이에도 끼어들어서
      // 줄 간격이 넓어 보이던 문제 - 세션 줄들을 별도 컨테이너(gap 없음)로
      // 묶어 카드 레벨 gap의 영향을 안 받게 한다.
      const sessionsBox = el("div", { style: { display: "flex", flexDirection: "column" } });
      [["전일 종가", s.prev, false], ["프리마켓", s.pre, s.active === "pre"],
       ["정규장", s.regular, s.active === "regular"], ["애프터", s.post, s.active === "post"]]
        .forEach(([label, val, active]) => {
          // ★ 지금 세션이 아닌 값은 참고용이라 작게, 줄 간격도 좁게 - 지금
          // 봐야 할 값(활성 세션)만 두드러지게 한다.
          const line = el("div", {
            style: {
              display: "flex", justifyContent: "space-between",
              fontWeight: active ? "700" : "400",
              fontSize: active ? "13px" : "11px",
              lineHeight: "1.3",
              padding: active ? "1px 0" : "0",
              color: active ? "" : "var(--muted)",
            },
          });
          line.appendChild(el("span", { text: label }));
          line.appendChild(el("span", {
            class: active ? dir(row.diff) : "",
            text: val != null ? Number(val).toLocaleString("ko-KR") : "-",
          }));
          sessionsBox.appendChild(line);
        });
      card.appendChild(sessionsBox);
    } else {
      card.appendChild(el("div", {
        style: { fontSize: "20px", fontWeight: "600" }, class: dir(row.diff),
        text: row.last != null ? Number(row.last).toLocaleString("ko-KR", { maximumFractionDigits: row.dp }) : "-",
      }));
    }

    if (row.diff != null) {
      const arrow = row.diff > 0 ? "▲" : row.diff < 0 ? "▼" : "-";
      card.appendChild(el("div", {
        class: dir(row.diff),
        text: `${arrow} ${Math.abs(row.diff).toLocaleString("ko-KR")} (${row.pct != null ? pct(row.pct) : "-"})`,
      }));
    }

    // ★★★ "시세를 가져온 시간과 출처는 제목이나 상단에 하나로 통합해" -
    // 예전엔 카드마다 출처(네이버/야후/업비트 등)를 매번 반복해서
    // 찍었다. 시각과 같은 원칙(상단에 기준을 한 번 보여주고, 카드에는
    // 그 기준과 실제로 다른 경우만 표시)을 출처에도 적용한다.
    let timeNote = "";
    if (!row.at) {
      timeNote = " · 시각 정보 없음";
    } else if (row.at !== _marketReferenceTime) {
      timeNote = " · " + row.at + "(기준과 다름)";
    }
    let srcNote = "";
    if (row.src && row.src !== _marketReferenceSource) {
      srcNote = " · " + row.src;
    }
    card.appendChild(el("div", { class: "hint", text: `${row.status}${srcNote}${timeNote}` }));
    return card;
  }

  let _marketReferenceTime = null;
  let _marketReferenceSource = null;

  // ★★★ "시장 메뉴도 그룹별로 묶어서 탭 형태로" - 7개 그룹이 한 페이지에 길게 쌓여 있어 원하는
  // 곳까지 스크롤해야 했다. 성격이 비슷한 그룹끼리 묶어 탭으로 나눈다. 서버가 새 그룹을
  // 추가해도 사라지지 않게 목록에 없는 그룹은 "기타" 탭에 모은다.
  const MARKET_TABS = [
    { id: "kr", label: "🇰🇷 국내", groups: ["국내"] },
    { id: "world", label: "🌐 해외·선물", groups: ["해외", "선물"] },
    { id: "us", label: "🇺🇸 미국 종목", groups: ["미국 대형주", "미국-HBM"] },
    { id: "fx", label: "💱 환율·코인", groups: ["환율", "암호화폐"] },
  ];
  let _marketTab = "kr";

  function _marketIcon(name) {
    return (window.UI && window.UI.icon) ? window.UI.icon(name) : "";
  }

  function renderMarket(data) {
    const panel = $('.panel[data-panel="market"]');
    // ★ 이 패널은 폴링마다 통째로 지우고 다시 그린다(카드 그리드가 조회
    // 조건에 따라 통째로 바뀌기 때문) - 문서 전체 스크롤을 보존한다.
    const _scrollEl = _scrollAnchorEl();
    const _savedTop = _scrollEl.scrollTop;
    panel.innerHTML = "";
    const head = el("div", { class: "page-head" });
    head.appendChild(el("h1", { html: _marketIcon("globe") + " 시장" }));
    panel.appendChild(head);
    // (data.note 는 긴 안내문이라 아래 기준 줄의 ⓘ 로 옮긴다)

    // ★★★ "일부 항목에 시간이 누락되어 있다" - 카드마다 각자 시각을
    // 따로 찍다 보니 어떤 건 있고 어떤 건 없어 지저분했다. 가장 흔한
    // 시각을 "기준 시간"으로 뽑아 상단에 한 번만 보여주고, 카드에는
    // 그 기준과 실제로 다른 경우(지연·오류 등)만 별도로 표기한다.
    const allTimes = data.groups.flatMap((g) => g.rows.map((r) => r.at)).filter(Boolean);
    const timeCounts = {};
    allTimes.forEach((t) => { timeCounts[t] = (timeCounts[t] || 0) + 1; });
    const referenceTime = Object.keys(timeCounts).sort((a, b) => timeCounts[b] - timeCounts[a])[0] || null;
    _marketReferenceTime = referenceTime;

    // ★ 출처도 시각과 같은 방식으로 - 가장 흔한 출처를 "기준"으로 뽑고,
    // 실제로 쓰인 모든 출처는 목록으로 한 줄에 같이 보여준다.
    const allSources = data.groups.flatMap((g) => g.rows.map((r) => r.src)).filter(Boolean);
    const sourceCounts = {};
    allSources.forEach((s) => { sourceCounts[s] = (sourceCounts[s] || 0) + 1; });
    const distinctSources = Object.keys(sourceCounts).sort((a, b) => sourceCounts[b] - sourceCounts[a]);
    _marketReferenceSource = distinctSources[0] || null;

    if (referenceTime || distinctSources.length) {
      const parts = [];
      if (referenceTime) parts.push(`기준 시각: ${referenceTime}`);
      if (distinctSources.length) parts.push(`출처: ${distinctSources.join(", ")}`);
      const info = el("div", { class: "hint" });
      info.appendChild(el("b", { text: parts.join(" · ") }));
      info.appendChild(infoIcon((data.note ? data.note + "\n\n" : "")
        + "아래 카드는 이 기준입니다. 기준과 다른 경우(지연·오류 등)만 카드에 따로 표시됩니다."));
      panel.appendChild(info);
    } else if (data.note) {
      panel.appendChild(el("div", { class: "hint", text: data.note }));
    }

    const byName = {};
    data.groups.forEach((g) => { byName[g.group] = g; });
    const tabs = MARKET_TABS
      .map((t) => ({ id: t.id, label: t.label, groups: t.groups.filter((n) => byName[n]).map((n) => byName[n]) }))
      .filter((t) => t.groups.length);
    const known = new Set(MARKET_TABS.reduce((acc, t) => acc.concat(t.groups), []));
    const extra = data.groups.filter((g) => !known.has(g.group));
    if (extra.length) tabs.push({ id: "etc", label: "기타", groups: extra });
    if (!tabs.some((t) => t.id === _marketTab) && tabs.length) _marketTab = tabs[0].id;

    // 탭 + 갱신 설정을 한 줄에 - 탭을 눌러도 다시 조회하지 않고 이미 받은 값을 보여준다.
    const bar = el("div", { class: "filter-row", style: { margin: "var(--s2) 0" } });
    const seg = el("div", { class: "seg" });
    const body = el("div");
    const paintBody = () => {
      body.innerHTML = "";
      const tab = tabs.find((t) => t.id === _marketTab);
      if (!tab) return;
      // ★★ 같은 error_code 로 카드 여러 개가 한꺼번에 실패하면(예: 네이버
      // 배치 자체가 막힘) 카드마다 같은 문구를 반복하지 않고 배너 하나로
      // 묶는다 - 3개 이상이면 "많다"고 본다.
      const rows = tab.groups.flatMap((g) => g.rows);
      const byCode = {};
      rows.forEach((r) => {
        if (!r.error || !r.error_code) return;
        const c = (byCode[r.error_code] = byCode[r.error_code] || { msg: r.error, n: 0 });
        c.n += 1;
      });
      const suppressedCodes = new Set(Object.keys(byCode).filter((code) => byCode[code].n >= 3));
      suppressedCodes.forEach((code) => {
        const c = byCode[code];
        body.appendChild(el("div", {
          class: "banner warn",
          text: `${c.msg} - ${c.n}건 조회 실패`,
        }));
      });
      tab.groups.forEach((g) => {
        // ★ 그룹이 탭으로 나뉘어 있어 그룹 제목은 뺀다(카드마다 이름이 있다).
        const grid = el("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(196px, 1fr))", gap: "var(--s2)", marginBottom: "var(--s3)" } });
        g.rows.forEach((row) => grid.appendChild(renderMarketCard(row, suppressedCodes)));
        body.appendChild(grid);
      });
    };
    tabs.forEach((t) => {
      seg.appendChild(el("button", {
        text: t.label, class: t.id === _marketTab ? "active" : "",
        onclick: (e) => {
          _marketTab = t.id;
          $$("button", seg).forEach((b) => b.classList.remove("active"));
          e.target.classList.add("active");
          paintBody();
        },
      }));
    });
    bar.appendChild(seg);
    const sel = el("select", { style: { marginLeft: "auto" } });
    data.refresh_choices.forEach((sec) => {
      sel.appendChild(el("option", { value: String(sec), text: sec === 0 ? "자동 갱신 끔" : `${sec}초마다` }));
    });
    const saved = localStorage.getItem("market.refresh");
    sel.value = saved || String(data.default_refresh);
    sel.addEventListener("change", () => {
      localStorage.setItem("market.refresh", sel.value);
      loadMarket(false);
      _scheduleMarketRefresh();
    });
    bar.appendChild(sel);
    bar.appendChild(el("button", { class: "b ghost small", html: `${_marketIcon("refresh")} 지금 갱신`, onclick: () => loadMarket(true) }));
    panel.appendChild(bar);
    panel.appendChild(body);
    paintBody();

    if (data.errors && data.errors.length) {
      panel.appendChild(el("div", { class: "banner warn", text: data.errors.join(" · ") }));
    }
    _restoreScrollAfter(_savedTop, _scrollEl);
  }

  let _marketTimer = null;

  async function loadMarket(refresh) {
    try {
      const ttl = Number(localStorage.getItem("market.refresh")) || undefined;
      const qs = new URLSearchParams();
      if (refresh) qs.set("refresh", "1");
      if (ttl) qs.set("ttl", String(ttl));
      const data = await api("/api/market?" + qs.toString());
      renderMarket(data);
    } catch (e) {
      toast(e.message, "error");
    }
  }

  function _scheduleMarketRefresh() {
    if (_marketTimer) clearInterval(_marketTimer);
    const sec = Number(localStorage.getItem("market.refresh")) || 20;
    if (sec <= 0) return;
    _marketTimer = setInterval(() => {
      if (!document.hidden && _activeTabIs("market")) loadMarket(false);
    }, sec * 1000);
  }

  registerPanel("market", {
    onShow: () => {
      loadMarket(false);
      _scheduleMarketRefresh();
    },
  });

  // ━━ 월간 리뷰 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  let _reviewMonth = null;
  let _reviewGroup = "virtual";

  function renderReviewSummary(stats) {
    const o = stats.overall;
    const strip = el("div", { class: "strip" });
    const cells = [
      ["월 손익", signed(o.pnl, "won"), dir(o.pnl)],
      ["승률", pct0(o.win_rate), ""],
      ["기대값", signed(o.expectancy, "won"), dir(o.expectancy)],
      ["손익비", o.profit_factor == null ? "∞" : o.profit_factor.toFixed(2), ""],
      ["최대낙폭", signed(o.mdd, "won"), dir(o.mdd)],
      ["평균 보유", Math.round(stats.holds.avg) + "분", ""],
    ];
    cells.forEach(([label, val, cls]) => {
      const cell = el("div", { class: "cell" });
      cell.appendChild(el("div", { class: "label", text: label }));
      cell.appendChild(el("div", { class: "value " + cls, text: val }));
      strip.appendChild(cell);
    });
    return strip;
  }

  function renderAngleTable(title, byMap, minTrades) {
    const details = el("details");
    const rows = Object.entries(byMap || {}).map(([k, s]) => ({
      key: k, label: s.label || k, trades: s.trades, pnl: s.pnl,
      win_rate: s.win_rate, expectancy: s.expectancy, enough: s.trades >= minTrades,
    }));
    details.appendChild(el("summary", { text: `${title} (${rows.length}개)` }));
    const gridBox = el("div");
    new DataGrid(gridBox, {
      columns: [
        { key: "label", label: "구분" },
        { key: "trades", label: "거래", fmt: (v, r) => (r.enough ? String(v) : `부족 — ${v}건, 판단 보류`), numeric: true },
        { key: "pnl", label: "손익", fmt: (v) => signed(v, "won"), bar: true, numeric: true },
        { key: "win_rate", label: "승률", fmt: (v) => pct0(v), numeric: true },
        { key: "expectancy", label: "기대값", fmt: (v) => signed(v, "won"), numeric: true },
      ],
      rows, page: 0, storageKey: "grid.review." + title,
    });
    details.appendChild(gridBox);
    return details;
  }

  function renderProposals(proposals) {
    const box = el("div");
    const checks = {};
    proposals.forEach((p) => {
      const card = el("div", { class: "card", style: { marginBottom: "var(--s2)" } });
      const head = el("div", { style: { display: "flex", gap: "var(--s2)", alignItems: "center" } });
      if (p.kind === "config") {
        const cb = el("input", { type: "checkbox" });
        checks[p.id] = cb;
        head.appendChild(cb);
      } else {
        head.appendChild(el("span", { class: "badge tech", style: { cursor: "default" }, text: "참고" }));
      }
      head.appendChild(el("b", { text: p.label }));
      card.appendChild(head);
      if (p.kind === "config") {
        card.appendChild(el("div", { text: `${JSON.stringify(p.current)} → ${JSON.stringify(p.proposed)}` }));
      }
      card.appendChild(el("div", { text: p.reason }));
      card.appendChild(el("div", { class: "hint", text: `확신: ${p.confidence}` }));
      card.appendChild(el("div", { class: "banner warn", text: "⚠ " + p.risk }));
      box.appendChild(card);
    });
    return { box, checks };
  }

  function renderIdeas(ideas) {
    const details = el("details");
    details.appendChild(el("summary", { html: `자체 최적화안 (${ideas.length}) <span class="hint">— 검증 안 됨</span>` }));
    ideas.forEach((idea) => {
      const card = el("div", { class: "card banner warn", style: { marginTop: "var(--s2)" } });
      card.appendChild(el("div", { html: `<b>${esc(idea.label)}</b> <span class="hint">기반: ${esc(idea.base)}</span>` }));
      card.appendChild(el("div", { text: idea.idea }));
      card.appendChild(el("div", { html: `<b>왜 자체안인가</b>: ${esc(idea.why)}` }));
      card.appendChild(el("div", { html: `<b>기대</b>: ${esc(idea.expect)}` }));
      card.appendChild(el("div", { html: `<b>확인 방법</b>: ${esc(idea.check)}` }));
      card.appendChild(el("div", { html: `<b>${esc(idea.status)}</b>` }));
      details.appendChild(card);
    });
    return details;
  }

  function renderReviewHistory(entries) {
    const box = el("div");
    box.appendChild(el("h2", { text: "반영 이력" }));
    if (!entries.length) {
      box.appendChild(el("div", { class: "hint", text: "이 달에 반영한 이력이 없습니다." }));
      return box;
    }
    entries.slice().reverse().forEach((e) => {
      const row = el("div", { class: "card", style: { marginBottom: "var(--s2)" } });
      // ★ ISO 문자열 그대로("2026-09-09T15:00:00.123456+09:00")는 읽기
      //   어렵다 - 날짜와 시:분:초로 나눠 보여준다.
      const when = String(e.at || "").replace("T", " ").slice(0, 19);
      row.appendChild(el("div", { html: `<b>${e.action === "apply" ? "반영" : "되돌림"}</b> ${esc(when)} (${esc(e.month)})` }));
      (e.changes || []).forEach((c) => row.appendChild(el("div", { text: `${c.label}: ${JSON.stringify(c.before)} → ${JSON.stringify(c.after)}` })));
      if (e.action === "apply") {
        row.appendChild(el("button", {
          class: "b quiet-danger small", text: "되돌리기",
          onclick: async () => {
            if (!confirm("이 변경을 되돌릴까요?")) return;
            try {
              await api("/api/review/revert", { method: "POST", body: { id: e.id } });
              toast("되돌렸습니다.");
              onShowReview();
            } catch (err) {
              toast(err.message, "error");
            }
          },
        }));
      }
      box.appendChild(row);
    });
    return box;
  }

  async function onShowReview() {
    const panel = $('.panel[data-panel="review"]');
    panel.innerHTML = "";
    panel.appendChild(skeleton(200));
    try {
      const monthsData = await api(`/api/review/months?group=${_reviewGroup}`);
      panel.innerHTML = "";

      const toolbar = el("div", { style: { display: "flex", gap: "var(--s2)", marginBottom: "var(--s2)" } });
      const monthSel = el("select");
      monthsData.months.forEach((m) => monthSel.appendChild(el("option", { value: m, text: m })));
      if (_reviewMonth && monthsData.months.includes(_reviewMonth)) monthSel.value = _reviewMonth;
      monthSel.addEventListener("change", () => {
        _reviewMonth = monthSel.value;
        onShowReview();
      });
      toolbar.appendChild(monthSel);

      const groupSel = el("select");
      [["virtual", "연습·모의"], ["live", "실거래"]].forEach(([v, l]) => groupSel.appendChild(el("option", { value: v, text: l })));
      groupSel.value = _reviewGroup;
      groupSel.addEventListener("change", () => {
        _reviewGroup = groupSel.value;
        _reviewMonth = null;
        onShowReview();
      });
      toolbar.appendChild(groupSel);
      panel.appendChild(toolbar);

      const month = _reviewMonth || monthSel.value;
      if (!month) {
        panel.appendChild(el("div", { class: "hint", text: "거래 기록이 없습니다." }));
        return;
      }
      _reviewMonth = month;

      const data = await api(`/api/review/monthly?month=${month}&group=${_reviewGroup}`);
      if (data.stats.empty) {
        panel.appendChild(el("div", { class: "hint", text: data.note || "이 달은 거래 기록이 없습니다." }));
        return;
      }

      panel.appendChild(renderReviewSummary(data.stats));
      if (data.note) panel.appendChild(el("div", { class: "banner warn", text: data.note }));

      // ★★★ "심플&모던, 스크롤 많은 네비게이션 지양" - 예전엔 요약 아래로 각도별
      // 분석 표 5개 · 제안 카드 · 자체안 · 반영 이력 · 암호화폐/스윙 요약까지 한
      // 화면에 전부 이어 붙어 있었다. 세 묶음으로 나눠 탭 하나만 보이게 한다
      // (renderTabs 는 첫 화면부터 항상 있던 공용 헬퍼다).
      const breakdownBox = el("div");
      breakdownBox.appendChild(renderAngleTable("기법별", data.stats.by_technique, 12));
      breakdownBox.appendChild(renderAngleTable("청산사유별", data.stats.by_reason, 12));
      breakdownBox.appendChild(renderAngleTable("시간대별", data.stats.by_slot, 8));
      breakdownBox.appendChild(renderAngleTable("테마별", data.stats.by_theme, 12));
      breakdownBox.appendChild(renderAngleTable("요일별", data.stats.by_weekday, 12));

      const proposalsBox = el("div");
      if (!data.proposals.length) {
        proposalsBox.appendChild(el("div", { class: "hint", text: "지금은 제안할 것이 없습니다." }));
      } else {
        const { box, checks } = renderProposals(data.proposals);
        proposalsBox.appendChild(box);

        const applyBar = el("div", { class: "card" });
        const noteInput = el("input", { type: "text", placeholder: "메모(선택)" });
        applyBar.appendChild(noteInput);
        applyBar.appendChild(el("button", {
          class: "b", text: "고른 항목 반영",
          onclick: async () => {
            const ids = Object.entries(checks).filter(([, cb]) => cb.checked).map(([id]) => id);
            if (!ids.length) {
              toast("고른 항목이 없습니다.");
              return;
            }
            if (!confirm(`${ids.length}건을 반영합니다. 계속할까요?`)) return;
            try {
              await api("/api/review/apply", { method: "POST", body: { month, group: _reviewGroup, ids, note: noteInput.value } });
              toast("반영했습니다.");
              onShowReview();
            } catch (e) {
              toast(e.message, "error");
            }
          },
        }));
        proposalsBox.appendChild(applyBar);
      }
      proposalsBox.appendChild(renderIdeas(data.ideas));

      const histBox = el("div");
      const histData = await api("/api/review/history");
      histBox.appendChild(renderReviewHistory((histData.entries || []).filter((e) => e.month === month)));

      // ★ 암호화폐는 review.py(최적화 제안 9종 등 국내주식 전용 로직)를 거치지
      // 않는다 - 완전히 다른 파일(crypto_state.json)에서 그 달 것만 걸러
      // 간단한 요약만 덧붙인다.
      try {
        const cryptoData = await api("/api/crypto/journal");
        const monthRows = (cryptoData.rows || []).filter((r) => {
          if (!r.exit_time) return false;
          const d = new Date(r.exit_time * 1000);
          const ym = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
          return ym === month;
        });
        if (monthRows.length) {
          const totalPnl = monthRows.reduce((a, r) => a + (r.pnl || 0), 0);
          const wins = monthRows.filter((r) => (r.pnl || 0) > 0).length;
          const box = el("div", { class: "card", style: { marginTop: "var(--s3)" } });
          box.appendChild(el("h2", { text: `${month} 암호화폐 요약` }));
          box.appendChild(el("div", {
            html: `거래 ${monthRows.length}건 · 승률 ${pct0(wins / monthRows.length)} · 손익 `
              + `<span class="${dir(totalPnl)}">${signed(totalPnl, "won")}</span>`,
          }));
          histBox.appendChild(box);
        }
      } catch (e) {
        /* 조용히 건너뛴다 - 국내주식 리뷰는 이미 다 보여줬다. */
      }

      // ★ 스윙도 암호화폐와 같은 이유로 완전히 다른 파일(swing_state.json)에서 그 달 것만 걸러
      // 간단한 요약만 덧붙인다.
      try {
        const swingData = await api("/api/swing/journal");
        const monthRows = (swingData.rows || []).filter((r) => {
          if (!r.exit_time) return false;
          const d = new Date(r.exit_time * 1000);
          const ym = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
          return ym === month;
        });
        if (monthRows.length) {
          const totalPnl = monthRows.reduce((a, r) => a + (r.pnl || 0), 0);
          const wins = monthRows.filter((r) => (r.pnl || 0) > 0).length;
          const box = el("div", { class: "card", style: { marginTop: "var(--s3)" } });
          box.appendChild(el("h2", { text: `${month} 스윙 요약` }));
          box.appendChild(el("div", {
            html: `거래 ${monthRows.length}건 · 승률 ${pct0(wins / monthRows.length)} · 손익 `
              + `<span class="${dir(totalPnl)}">${signed(totalPnl, "won")}</span>`,
          }));
          histBox.appendChild(box);
        }
      } catch (e) {
        /* 조용히 건너뛴다. */
      }

      renderTabs(panel, "review", [
        { id: "breakdown", label: "각도별 분석", build: () => breakdownBox },
        { id: "proposals", label: `제안 (${data.proposals.length})`, build: () => proposalsBox },
        { id: "history", label: "반영 이력", build: () => histBox },
      ]);
    } catch (e) {
      panel.innerHTML = "";
      toast(e.message, "error");
    }
  }

  registerPanel("review", { onShow: onShowReview });

  // ━━ 실험실 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  let _labTimer = null;

  function renderLabParticipantCard(p) {
    const card = el("div", { class: "card" });
    const head = el("div", { style: { display: "flex", justifyContent: "space-between" } });
    head.appendChild(el("span", { html: `<b>${esc(p.name)}</b>` }));
    head.appendChild(el("span", {
      class: "badge tech",
      style: { cursor: "default", background: p.real ? "var(--rise)" : "var(--card)", color: p.real ? "#fff" : "var(--ink)" },
      text: p.real ? "실제 주문" : "가상 체결",
    }));
    card.appendChild(head);
    if (p.note) card.appendChild(el("div", { class: "hint", text: p.note }));
    card.appendChild(el("div", { text: `상태: ${p.status || "-"}` }));
    card.appendChild(el("div", {
      text: `거래 ${p.trades ?? 0} · 실현손익 ${signed(p.realized_pnl ?? p.pnl ?? 0, "won")} · 보유 ${p.positions ?? "-"}`,
    }));
    if (p.win_rate != null) {
      card.appendChild(el("div", {
        text: `승률 ${pct0(p.win_rate)} · 기대값 ${signed(p.expectancy, "won")} · `
          + `손익비 ${p.profit_factor == null ? "∞" : p.profit_factor.toFixed(2)} · MDD ${signed(p.mdd, "won")}`,
      }));
    }
    if (p.error) card.appendChild(el("div", { class: "banner danger", text: p.error }));
    return card;
  }

  // ━━ 종목별·테마별 매매기법 백테스트 - 오늘 선정된 테마 종목에 최근 실제 시세를 기법별로
  // 재생해 손익을 비교한다. 결과는 저장되어 실전 매매 진입 기법 채점에 가산점으로 반영된다
  // (daytrader/technique_backtest.py·technique_prefs.py·playbook.py 참고). ━━━━━━━━━━━━━━━━
  function renderTechniqueBacktestCard() {
    const card = el("div", { class: "card" });
    const title = _pcIconEl("h2", "flask", "종목별 매매기법 백테스트");
    title.appendChild(infoIcon(
      "오늘 선정된 테마 종목에 최근 실제 시세(1분봉)를 기법별로 독립 재생해, 이 종목에서 어느 "
      + "기법이 더 잘 맞았는지 순위를 매깁니다.\n\n"
      + "청산은 모든 기법에 공통으로 지금 설정된 손절·익절·추적손절만 씁니다(진입 신호끼리 공정하게 "
      + "비교하기 위해서입니다) - ATR 손절 등 고급 청산이나 분할 매수·매도는 재현하지 않습니다.\n\n"
      + "'결과를 실전 매매에 반영'을 켜 두면(기본 켜짐), 이후 실전 매매(모의·실거래)가 이 종목·테마의 "
      + "진입 기법을 고를 때 여기서 이긴 기법에 가산점을 줍니다(강제는 아니고, 신호가 없으면 그래도 안 삽니다).\n\n"
      + "★ 국내·해외·암호화폐 모두 종목이 새로 선정될 때마다(재선정) 이 백테스트가 자동으로도 돌아 결과를 "
      + "저장합니다(하루 한 번, 같은 종목 조합이면 다시 안 돎) - 아래 버튼은 지금 바로 확인하고 싶을 때 씁니다."));
    card.appendChild(title);

    const row = el("div", { style: { display: "flex", gap: "var(--s2)", flexWrap: "wrap", alignItems: "center", marginTop: "var(--s2)" } });
    const marketSel = el("select");
    [["domestic", "국내주식"], ["overseas", "해외주식"], ["crypto", "암호화폐"], ["swing", "스윙"]].forEach(([v, l]) => marketSel.appendChild(el("option", { value: v, text: l })));
    const daysInput = el("input", { type: "number", value: "7", min: "1", max: "30", style: { width: "56px" } });
    // ★ 스윙은 일봉(하루=봉 하나)이라 "최근 며칠"의 의미가 다르다 - 60일선 웜업만도 30일보다
    // 훨씬 많이 필요해서(daytrader/technique_backtest.py 참고) 기본값·상한을 크게 바꿔 준다.
    marketSel.addEventListener("change", () => {
      if (marketSel.value === "swing") {
        daysInput.max = "400";
        daysInput.value = "240";
      } else {
        daysInput.max = "30";
        if (Number(daysInput.value) > 30) daysInput.value = "7";
      }
    });
    const saveCb = el("input", { type: "checkbox", checked: true });
    const runBtn = el("button", { class: "b", text: "실행" });
    row.append(
      el("span", { text: "시장" }), marketSel,
      el("span", { text: "최근" }), daysInput, el("span", { text: "일(스윙은 거래일)" }),
      el("label", { style: { display: "flex", alignItems: "center", gap: "4px" } }, [saveCb, el("span", { text: "결과를 실전 매매에 반영" })]),
      runBtn,
    );
    card.appendChild(row);

    const resultBox = el("div");
    card.appendChild(resultBox);

    runBtn.onclick = async () => {
      runBtn.disabled = true;
      const origText = runBtn.textContent;
      runBtn.textContent = "실행 중... (몇 초~수십 초 걸릴 수 있습니다)";
      resultBox.innerHTML = "";
      try {
        const result = await api("/api/lab/technique_backtest", {
          method: "POST",
          body: { market: marketSel.value, days: Number(daysInput.value) || 7, save: !!saveCb.checked },
        });
        renderTechniqueBacktestResult(resultBox, result);
      } catch (e) {
        toast(e.message, "error");
      } finally {
        runBtn.disabled = false;
        runBtn.textContent = origText;
      }
    };
    return card;
  }

  function renderTechniqueBacktestResult(box, result) {
    box.innerHTML = "";
    (result.errors || []).forEach((e) => box.appendChild(el("div", { class: "hint fall", text: "· " + e })));
    if (!result.by_symbol || !result.by_symbol.length) {
      box.appendChild(el("div", { class: "hint", text: "오늘 선정된 후보가 없거나 시세를 받지 못했습니다." }));
      return;
    }
    box.appendChild(el("div", { class: "hint", text: `${result.at || ""} · 후보 ${result.candidates}종목 · 최근 ${result.days}일치 시세` }));

    const cols = [
      { label: "기법", render: (r) => (r.best ? el("b", { text: "★ " + r.label }) : r.label) },
      { label: "거래", cls: "num", render: (r) => r.trades },
      { label: "승률", cls: "num", render: (r) => (r.trades ? pct0(r.win_rate) : "-") },
      { label: "총손익", cls: "num", render: (r) => (r.trades ? el("span", { class: dir(r.total_pnl_pct), text: pct(r.total_pnl_pct) }) : "-") },
      { label: "손익비", cls: "num", render: (r) => (!r.trades ? "-" : r.profit_factor == null ? "∞(손실 없음)" : r.profit_factor.toFixed(2)) },
    ];
    result.by_symbol.forEach((row) => {
      const sub = el("div", { class: "card", style: { marginTop: "var(--s2)" } });
      sub.appendChild(el("div", { html: `<b>${esc(row.name)}</b> (${esc(row.symbol)})` + (row.theme ? ` · ${esc(row.theme)}` : "") + ` · 봉 ${row.bars}개` }));
      const rows = row.results.map((r) => ({ ...r, best: r.technique === row.best_technique }));
      sub.appendChild(_miniTable(cols, rows));
      box.appendChild(sub);
    });

    if (result.by_theme && result.by_theme.length) {
      // ★ "매매기법 테스트 결과를 가시성있게" - 예전엔 테마 하나당 기법 결과를 전부 한 줄
      // 텍스트로 이어 붙여서(예: "기법A(+1%, 3종목) · 기법B(-2%, 1종목) · ...") 기법이
      // 많아지면 줄이 너무 길어져 비교가 힘들었다. 종목별 표와 같은 표 형태로 바꾸고,
      // 손익도 색으로 구분한다(이익 빨강/손실 파랑 - 손익 표시 공통 규칙).
      const themeCard = el("div", { class: "card", style: { marginTop: "var(--s2)" } });
      themeCard.appendChild(el("h3", { text: "테마별 평균(종목별로 기법이 다르게 나왔는지 한눈에)" }));
      result.by_theme.forEach((t) => {
        const sub = el("div", { style: { marginTop: "var(--s2)" } });
        sub.appendChild(el("div", { html: `<b>${esc(t.theme)}</b>` }));
        const rows = [...t.results].sort((a, b) => b.avg_pnl_pct - a.avg_pnl_pct);
        const best = rows[0];
        const tcols = [
          { label: "기법", render: (r) => (r === best ? el("b", { text: "★ " + r.technique }) : r.technique) },
          { label: "평균손익", cls: "num", render: (r) => el("span", { class: dir(r.avg_pnl_pct), text: pct(r.avg_pnl_pct) }) },
          { label: "종목수", cls: "num", render: (r) => r.symbols },
        ];
        sub.appendChild(_miniTable(tcols, rows));
        themeCard.appendChild(sub);
      });
      box.appendChild(themeCard);
    }
  }

  // ★ 종목이 새로 선정될 때(재선정) 엔진이 백그라운드에서 자동으로도 돌린다(하루 한 번, 같은
  // 종목 조합이면 다시 안 돎 - daytrader/auto_backtest.py). 수동으로 누른 것과 자동으로 돈 것을
  // 모두 최신순으로 보여준다.
  async function renderTechniqueBacktestHistoryCard() {
    const card = el("div", { class: "card" });
    card.appendChild(_pcIconEl("h2", "layers", "최근 실행 기록"));
    card.appendChild(el("div", {
      class: "hint",
      text: "종목이 새로 선정될 때마다 자동으로도 실행됩니다(하루 한 번, 같은 종목 조합이면 다시 돌지 않음) - 위에서 직접 실행할 수도 있습니다.",
    }));
    const box = el("div", { style: { marginTop: "var(--s2)" } });
    card.appendChild(box);
    try {
      const { history } = await api("/api/lab/technique_backtest/history?limit=20");
      if (!history.length) {
        box.appendChild(el("div", { class: "hint", text: "아직 실행 기록이 없습니다." }));
      } else {
        const marketLabel = { domestic: "국내주식", overseas: "해외주식", crypto: "암호화폐", swing: "스윙" };
        const cols = [
          { label: "시각", render: (r) => (r.at || "").replace("T", " ").slice(0, 19) },
          { label: "시장", render: (r) => marketLabel[r.market] || r.market },
          { label: "계기", render: (r) => (r.trigger === "auto" ? "자동" : "수동") },
          { label: "종목", cls: "num", render: (r) => `${r.picked}/${r.candidates}` },
          {
            label: "결과(종목→1등 기법)",
            // ★ "가시성있게" - 예전엔 종목이 여러 개면 쉼표로 이어 붙인 한 줄 텍스트라
            // 길어질수록 어디까지가 한 종목 결과인지 구분하기 힘들었다. 종목마다 줄바꿈되는
            // 조각(chip)으로 나누고 손익도 색으로 구분한다.
            render: (r) => {
              const items = (r.by_symbol || []).filter((x) => x.best_technique);
              if (!items.length) return el("span", { class: "hint", text: "신호 없음" });
              const wrap = el("div", { style: { display: "flex", flexWrap: "wrap", gap: "4px 10px" } });
              items.forEach((x) => {
                wrap.appendChild(el("span", {
                  html: `${esc(x.name)}→<b>${esc(x.best_technique)}</b> `
                    + `<span class="${dir(x.total_pnl_pct)}">${pct(x.total_pnl_pct)}</span>`,
                }));
              });
              return wrap;
            },
          },
        ];
        box.appendChild(_miniTable(cols, history));
      }
    } catch (e) {
      box.appendChild(el("div", { class: "hint fall", text: "불러오기 실패: " + e.message }));
    }
    return card;
  }

  async function loadLab() {
    const panel = $('.panel[data-panel="lab"]');
    // ★★★ "자동 갱신되면 화면이 맨 위로 튄다" - 이 함수는 패널을 통째로
    // 비운 뒤 여러 번 await 를 거쳐 다시 채운다(그 사이 문서가 잠깐 비어
    // 스크롤이 밀린다). try/finally 로 어느 경로로 끝나든 마지막에 같은
    // 위치로 되돌린다.
    const _scrollEl = _scrollAnchorEl();
    const _savedTop = _scrollEl.scrollTop;
    try {
    panel.innerHTML = "";

    // ★★★ "심플&모던, 스크롤 많은 네비게이션 지양" - 종목별 기법 백테스트(실행+기록)와
    // 가상 실험(설정 비교)은 서로 다른 용도라 늘 함께 볼 필요가 없다 - 탭으로 나눠
    // 한 번에 하나만 보여준다(renderTabs 는 다른 패널에서도 쓰는 공용 헬퍼).
    const backtestBox = el("div");
    backtestBox.appendChild(renderTechniqueBacktestCard());
    backtestBox.appendChild(await renderTechniqueBacktestHistoryCard());

    let data;
    try {
      data = await api("/api/lab");
    } catch (e) {
      toast(e.message, "error");
      renderTabs(panel, "lab", [{ id: "backtest", label: "종목별 기법 백테스트", build: () => backtestBox }]);
      return;
    }

    const expBox = el("div");
    const labWarn = el("div", { class: "banner warn" });
    labWarn.appendChild(_pcIconEl("span", "flask", "실험실 참가자는 전부 가상 체결입니다 - 실제 주문은 나가지 않습니다."));
    labWarn.appendChild(infoIcon(
      "실제 주문을 내는 엔진은 언제나 하나뿐입니다.\n\n"
      + "실험실에서 여러 설정을 동시에 돌려도 모두 가상 체결이며, "
      + "지금 돌고 있는 실거래 엔진에는 절대 주문을 내지 않습니다.\n\n"
      + "여기서 좋은 결과가 나온 설정을 실제로 쓰려면 [설정] 화면에서 직접 바꿔야 합니다."));
    expBox.appendChild(labWarn);

    if (data.running) {
      if (data.spec) {
        expBox.appendChild(el("div", { class: "hint", text: `실행 중: ${data.spec.kind === "shadow" ? "섀도 비교" : "대조 실험"} · 시드 ${data.spec.seed}` }));
        expBox.appendChild(el("div", { class: "hint", text: data.spec.fairness }));
      }
      const grid = el("div", { class: "cols" });
      data.participants.forEach((p) => grid.appendChild(renderLabParticipantCard(p)));
      expBox.appendChild(grid);
      expBox.appendChild(el("button", {
        class: "b quiet-danger", text: "실험 중지(가상만)",
        onclick: async () => {
          await api("/api/lab/stop", { method: "POST" });
          toast("중지했습니다.");
          loadLab();
        },
      }));
      renderTabs(panel, "lab", [
        { id: "backtest", label: "종목별 기법 백테스트", build: () => backtestBox },
        { id: "experiment", label: "가상 실험(실행 중)", build: () => expBox },
      ]);
      if (!_labTimer) {
        _labTimer = setInterval(() => {
          if (_activeTabIs("lab")) loadLab();
        }, 5000);
      }
      return;
    }
    if (_labTimer) {
      clearInterval(_labTimer);
      _labTimer = null;
    }

    expBox.appendChild(el("div", { class: "hint", text: data.how }));

    const presetBox = el("div", { class: "cols" });
    const checks = [];
    data.presets.forEach((p) => {
      const label = el("label", { class: "card", style: { display: "block" } });
      const cb = el("input", { type: "checkbox", value: p.id });
      checks.push(cb);
      label.appendChild(cb);
      label.appendChild(el("b", { text: " " + p.name }));
      label.appendChild(el("div", { class: "hint", text: p.note }));
      presetBox.appendChild(label);
    });
    expBox.appendChild(presetBox);

    const speedSel = el("select");
    [60, 600, 3600].forEach((s) => speedSel.appendChild(el("option", { value: String(s), text: `${s}배속` })));
    speedSel.value = "600";
    expBox.appendChild(speedSel);

    expBox.appendChild(el("button", {
      class: "b", text: "실험 시작",
      onclick: async () => {
        const chosen = checks.filter((cb) => cb.checked).map((cb) => cb.value);
        if (chosen.length > data.max_variants) {
          toast(`최대 ${data.max_variants}명까지 고를 수 있습니다.`, "error");
          return;
        }
        if (!data.can_shadow && chosen.length < 2) {
          toast("비교 대상이 없습니다 - 2개 이상 고르세요.", "error");
          return;
        }
        const variants = chosen.map((id) => {
          const preset = data.presets.find((p) => p.id === id);
          return { name: preset.name, overrides: preset.overrides, note: preset.note };
        });
        try {
          await api("/api/lab/start", { method: "POST", body: { variants, speed: Number(speedSel.value), start: "09:00" } });
          toast("실험을 시작했습니다.");
          loadLab();
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }));

    try {
      const result = await api("/api/lab/result");
      if (result.participants && result.participants.length) {
        expBox.appendChild(el("h2", { text: "지난 결과" }));
        expBox.appendChild(el("div", { class: "banner", text: result.verdict }));
        const gridBox = el("div");
        new DataGrid(gridBox, {
          columns: [
            { key: "name", label: "이름" },
            { key: "trades", label: "거래", numeric: true },
            { key: "pnl", label: "손익", fmt: (v) => signed(v, "won"), bar: true, numeric: true },
            { key: "win_rate", label: "승률", fmt: (v) => pct0(v), numeric: true },
            { key: "expectancy", label: "기대값", fmt: (v) => signed(v, "won"), numeric: true },
            { key: "profit_factor", label: "손익비", fmt: (v) => (v == null ? "∞" : v.toFixed(2)), numeric: true },
            { key: "mdd", label: "MDD", fmt: (v) => signed(v, "won"), numeric: true, tone: "pnl" },
          ],
          rows: result.participants, page: 0, storageKey: "grid.lab.result",
        });
        expBox.appendChild(gridBox);
        (result.caution || []).forEach((c) => expBox.appendChild(el("div", { class: "hint", text: "· " + c })));
      }
    } catch (e) {
      /* 지난 결과가 없으면 조용히 넘어간다. */
    }

    renderTabs(panel, "lab", [
      { id: "backtest", label: "종목별 기법 백테스트", build: () => backtestBox },
      { id: "experiment", label: "가상 실험", build: () => expBox },
    ]);
    } finally {
      _restoreScrollAfter(_savedTop, _scrollEl);
    }
  }

  registerPanel("lab", { onShow: loadLab });

  // ━━ 초기화 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function init() {
    buildNav();
    const initial = (location.hash || "").replace("#", "") || NAV[0].tabs[0][0];
    showTab(initial, { silent: true });
    startSSE();
    // ★ 화면을 열 때 미리 불러 둔다 - 기법 배지를 눌렀을 때 비어 있으면 안 된다.
    loadTechniques();

    // ★★★ "재빌드했는데 실제로 반영됐는지 화면만 봐도 알고 싶다" - 버전은
    // 켜져 있는 동안 안 바뀌니 실시간 스트림에 얹을 필요 없이 한 번만 받아
    // 상단에 표시한다. /api/status 는 어차피 재연결·인증 확인용으로도
    // 가벼우니 실패해도 조용히 넘어간다(버전 표시는 부가 정보일 뿐이다).
    api("/api/status").then((s) => {
      if (!s || !s.version) return;
      const v = $("#app-version");
      if (v) {
        v.textContent = "v" + s.version;
        v.style.display = "";
        v.style.cursor = "pointer";
        v.title = "릴리즈 노트 보기";
        v.addEventListener("click", () => showTab("release"));
      }
    }).catch(() => {});

    // ★ 상단 밴드(시장별 매매 모드)는 어느 탭에서든 최신이어야 한다 - 국내는 5초,
    //   암호화폐·해외는 조금 더 느리게 받는다(꺼져 있거나 실패하면 그 값은 그대로 둔다).
    const pollDomesticBand = async () => {
      try { _lastDomesticStatus = await api("/api/status"); } catch (e) { /* 다음 주기에 */ }
      updateBand();
    };
    const pollOtherBand = async () => {
      try { _lastCryptoStatus = await api("/api/crypto/status"); } catch (e) { /* 유지 */ }
      try { _lastOverseasStatus = await api("/api/overseas/status"); _captureUsdKrwRate(_lastOverseasStatus); } catch (e) { /* 유지 */ }
      try { _lastSwingStatus = await api("/api/swing/status"); } catch (e) { /* 유지 */ }
      updateBand();
    };
    pollDomesticBand(); pollOtherBand();
    setInterval(pollDomesticBand, 5000);
    setInterval(pollOtherBand, 15000);

    // ★ 모바일은 호버(title 툴팁)가 안 되니 눌러도 설명이 뜨게 한다.
    const connDot = $("#conn-indicator");
    if (connDot) {
      connDot.style.cursor = "pointer";
      connDot.addEventListener("click", () => {
        toast(CONN_EXPLAIN[CONN.mode] || "");
      });
    }
  }

  // ━━ 로그인 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★ 서버가 /api/* 를 전부 잠가 두므로 여기는 화면 전환만 담당한다 -
  // 진짜 잠금은 서버 쪽 미들웨어다(화면만 감추는 건 우회하기 쉽다).

  function showApp() {
    const overlay = $("#login-overlay");
    const root = $("#app-root");
    if (overlay) overlay.style.display = "none";
    if (root) root.style.display = "";
    init();
    // ★ 기본 비밀번호(123456)는 누구나 아는 값이라 로그인 벽이 사실상 없는 것과 같다 - 쓰고 있으면 계속 경고한다.
    api("/api/auth/check").then((r) => {
      if (r && r.default_password) _showDefaultPasswordBanner();
      _idleMinutes = (r && r.idle_minutes) || 0;
    }).catch(() => {});
  }

  function _showDefaultPasswordBanner() {
    const root = $("#app-root");
    if (!root || $("#default-pw-banner")) return;
    const b = el("div", {
      id: "default-pw-banner", class: "banner danger", style: { cursor: "pointer", margin: "var(--s2)" },
      text: "⚠ 기본 비밀번호(123456)를 쓰고 있습니다 - 누구나 아는 값입니다. 눌러서 [준비·연결] → 외부 연동에서 지금 바꾸세요.",
      onclick: () => { _tabState.setup = "keys"; showTab("setup"); },
    });
    root.insertBefore(b, root.firstChild);
  }

  function showLogin() {
    const overlay = $("#login-overlay");
    const root = $("#app-root");
    if (root) root.style.display = "none";
    if (overlay) overlay.style.display = "";
    if (typeof _stopDeviceWait === "function") _stopDeviceWait();  // 대기 중이었다면 폼으로 되돌린다
    const input = $("#login-password");
    if (input) {
      input.value = "";
      input.focus();
    }
  }

  // ━━ 민감한 동작 재확인(비밀번호) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★ [설정] 메뉴 진입, 자동매매 시작/정지처럼 되돌리기 어렵거나 민감한
  // 동작 전에 비밀번호를 한 번 더 확인한다. 로그인 세션이 이미 있어도,
  // 자리를 비운 사이 잠금 안 된 화면을 다른 사람이 주웠을 때의 방어선이다.
  // /api/login 을 그대로 재사용해서 서버 쪽엔 손댈 게 없다(맞으면 세션
  // 쿠키도 같이 갱신된다).

  let _pwConfirmOnOk = null;
  let _pwConfirmScope = "trade";
  let _pwConfirmOnCancel = null;

  function confirmPassword(onOk, opts) {
    opts = opts || {};
    _pwConfirmOnOk = onOk;
    _pwConfirmScope = opts.scope || "trade";
    _pwConfirmOnCancel = opts.onCancel || null;
    const overlay = $("#settings-guard-overlay");
    const title = $("#settings-guard-title");
    const hint = $("#settings-guard-hint");
    const input = $("#settings-guard-password");
    const errBox = $("#settings-guard-error");
    if (!overlay) {
      // ★ 모달 자체가 없으면(마크업 누락 등) 막지 않고 그냥 통과시킨다 -
      // 잠금 실패로 동작이 아예 안 되는 것보다는 낫다.
      if (onOk) onOk();
      return;
    }
    if (title) title.textContent = opts.title || "🔒 확인";
    if (hint) hint.textContent = opts.hint || "계속하려면 비밀번호를 다시 입력하세요.";
    if (errBox) errBox.style.display = "none";
    if (input) input.value = "";
    overlay.style.display = "flex";
    if (input) setTimeout(() => input.focus(), 50);
  }

  function closeSettingsGuard() {
    const overlay = $("#settings-guard-overlay");
    if (overlay) overlay.style.display = "none";
  }

  function wireSettingsGuard() {
    const form = $("#settings-guard-form");
    const cancelBtn = $("#settings-guard-cancel");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("#settings-guard-password");
      const errBox = $("#settings-guard-error");
      if (errBox) errBox.style.display = "none";
      try {
        // ★ 비밀번호가 맞으면 서버가 이 범위(trade/settings)의 재확인 토큰을 준다.
        const r = await api("/api/confirm", {
          method: "POST", body: { password: (input && input.value.trim()) || "", scope: _pwConfirmScope },
        });
        _confirmTokens[_pwConfirmScope] = r.token;
        _saveConfirmTokens();
        closeSettingsGuard();
        const onOk = _pwConfirmOnOk;
        _pwConfirmOnOk = null;
        if (onOk) onOk();
      } catch (err) {
        if (errBox) {
          errBox.textContent = (err && err.message) || "비밀번호가 올바르지 않습니다.";
          errBox.style.display = "";
        }
        if (input) {
          input.value = "";
          input.focus();
        }
      }
    });
    if (cancelBtn) {
      cancelBtn.addEventListener("click", () => {
        // ★ 호출부가 onOk() 를 실제 동작 직전에 두므로, 취소하면 모달만
        // 닫고 아무 것도 실행하지 않는다.
        closeSettingsGuard();
        _pwConfirmOnOk = null;
        const cb = _pwConfirmOnCancel;
        _pwConfirmOnCancel = null;
        if (cb) cb();
      });
    }
  }

  // 처음 보는 IP·기기 승인 대기 - 서버가 도는 PC의 /admin 에서 승인될 때까지 몇 초마다 물어본다.
  let _deviceWaitTimer = null;

  function _stopDeviceWait() {
    clearInterval(_deviceWaitTimer);
    _deviceWaitTimer = null;
    const form = $("#login-form");
    const wait = $("#login-wait");
    if (form) form.style.display = "";
    if (wait) wait.style.display = "none";
  }

  function _waitForDeviceApproval(token) {
    const form = $("#login-form");
    const wait = $("#login-wait");
    const msg = $("#login-wait-msg");
    if (form) form.style.display = "none";
    if (wait) wait.style.display = "";
    if (msg) msg.style.display = "none";
    clearInterval(_deviceWaitTimer);
    _deviceWaitTimer = setInterval(async () => {
      try {
        const r = await api(`/api/login/pending/${token}`);
        if (r.status === "pending") return;  // 계속 기다린다
        if (r.status === "approved") {
          _stopDeviceWait();
          if (_locked) { location.reload(); return; }
          showApp();
          return;
        }
        _stopDeviceWait();
        const errBox = $("#login-error");
        const text = r.status === "denied" ? "접속이 거부되었습니다." : "승인 대기 시간이 지났습니다. 다시 로그인해 주세요.";
        if (errBox) { errBox.textContent = text; errBox.style.display = ""; }
      } catch (e) {
        // 네트워크 오류 등 - 다음 번 폴링에서 다시 시도한다.
      }
    }, 3000);
  }

  function wireLoginForm() {
    const form = $("#login-form");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("#login-password");
      const errBox = $("#login-error");
      if (errBox) errBox.style.display = "none";
      try {
        const r = await api("/api/login", { method: "POST", body: { password: (input && input.value.trim()) || "" } });
        if (r && r.pending) {
          _waitForDeviceApproval(r.token);
          return;
        }
        if (_locked) { location.reload(); return; }  // 잠금 뒤 다시 로그인하면 화면을 깨끗이 다시 시작한다.
        showApp();
      } catch (err) {
        if (errBox) {
          errBox.textContent = (err && err.message) || "로그인에 실패했습니다.";
          errBox.style.display = "";
        }
        if (input) {
          input.value = "";
          input.focus();
        }
      }
    });
  }

  // 패널별 "이미 불러온 화면을 그대로 쓰는" 시간(ms). 실시간(대시보드·종목선정·속보·시장)은 제외.
  const PANEL_CACHE_MS = {
    perf: 60000, journal: 60000,
    playbook: 300000, rules: 300000, release: 600000,
    setup: 300000, themes: 300000, config: 300000, review: 300000, lab: 300000,
  };
  Object.keys(PANEL_CACHE_MS).forEach((id) => { if (_panels[id]) _panels[id].cacheMs = PANEL_CACHE_MS[id]; });

  function boot() {
    // ★ showTab() 의 설정 잠금 가드(_confirmTokens.settings 확인)가 참고할 수 있도록,
    // init() 이 새로고침 직후 해시(#config 등)로 바로 showTab() 을 부르기 전에 먼저 읽어 둔다.
    _loadConfirmTokens();
    wireLoginForm();
    wireSettingsGuard();
    api("/api/auth/check").then((r) => {
      if (r && r.authenticated) {
        showApp();
      } else {
        showLogin();
      }
    }).catch(() => {
      // ★ 확인 자체가 실패해도(네트워크 등) 일단 로그인 화면을 보여준다 -
      // 잠긴 화면을 그냥 통과시키는 것보다 안전한 쪽으로 기운다.
      showLogin();
    });
  }

  // ━━ 다크모드 토글 - 로그인 여부와 무관하게 켜져야 해서 boot() 과 함께
  // 초기화한다(밴드·버튼은 로그인 화면에서도 항상 DOM 에 있다). ━━━━━━━━━━
  const THEME_KEY = "ui.theme";

  function applyTheme(mode) {
    // mode 는 "light" · "dark" · null/undefined(시스템 설정 따름) 중 하나.
    if (mode === "light" || mode === "dark") {
      document.documentElement.setAttribute("data-theme", mode);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    const btn = $("#theme-toggle");
    if (btn) {
      const isDark = mode === "dark" || (!mode && window.matchMedia("(prefers-color-scheme: dark)").matches);
      // ★ 이모지(🌙/☀️) 대신 UI.icon() 선 아이콘 - 재디자인 지침대로
      // 아이콘 세트를 하나로 통일한다.
      btn.innerHTML = icon(isDark ? "sun" : "moon", 17);
      btn.setAttribute("aria-label", isDark ? "밝은 화면으로 전환" : "어두운 화면으로 전환");
    }
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) { /* 사생활 보호 모드 등 - 무시 */ }
    applyTheme(saved);
    const btn = $("#theme-toggle");
    if (btn) {
      btn.addEventListener("click", () => {
        const isDark = document.documentElement.getAttribute("data-theme") === "dark"
          || (!document.documentElement.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
        const next = isDark ? "light" : "dark";
        try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 무시 */ }
        applyTheme(next);
      });
    }
  }

  // ━━ 해외주식 통화 표시(달러/원화) 토글 - 다크모드 토글과 똑같은 구조(localStorage 에
  // 저장한 값을 읽어 버튼 표시를 맞추고, 클릭하면 뒤집어 저장·재적용). ━━━━━━━━━━━━━
  const CURRENCY_KEY = "ui.overseasCurrency";  // "usd" | "krw" - _currencyMode() 가 읽는다(위 ~812줄).

  function applyCurrencyMode(mode) {
    const btn = $("#currency-toggle");
    if (btn) btn.textContent = mode === "krw" ? "₩" : "$";
  }

  // ★ "해외주식이 포함된 곳에만 기능이 있으면돼" - 달러/원화 토글은 국내·암호화폐만
  // 보는 화면에 늘 떠 있으면 헷갈린다. 대시보드/종목선정/성과/매매일지는 자체 시장
  // 필터(_dashMarketFilter 등)가 있어 그 필터가 "통합"·"해외주식"일 때만 실제로 달러
  // 금액이 나오므로 그 두 값일 때만 보여준다. 준비·연결 탭은 계좌 현황에서 해외 계좌를
  // 볼 수 있어 탭 자체로 판단한다. 그 외(뉴스·시장·리뷰·설정 등) 탭은 달러 금액을 아예
  // 안 써서 숨긴다. showTab()으로 탭이 바뀔 때(_reallyShowTab)와, 대시보드/종목선정/
  // 성과/매매일지 안에서 시장 필터 세그먼트를 바꿀 때(renderMarketFilterSeg) 양쪽에서
  // 다시 계산한다.
  function _currencyToggleRelevant() {
    const showsOverseas = (f) => f === "all" || f === "overseas";
    switch (_activeTab) {
      case "dash": return showsOverseas(_dashMarketFilter);
      case "selection": return showsOverseas(_selectionMarketFilter);
      case "perf": return showsOverseas(_perfMarketFilter);
      case "journal": return showsOverseas(_journalMarketFilter);
      case "setup": return true;
      default: return false;
    }
  }

  function _updateCurrencyToggleVisibility() {
    const btn = $("#currency-toggle");
    if (btn) btn.style.display = _currencyToggleRelevant() ? "" : "none";
  }

  function initCurrencyToggle() {
    let saved = null;
    try { saved = localStorage.getItem(CURRENCY_KEY); } catch (e) { /* 사생활 보호 모드 등 - 무시 */ }
    const mode = saved === "krw" ? "krw" : "usd";
    applyCurrencyMode(mode);
    _updateCurrencyToggleVisibility();
    const btn = $("#currency-toggle");
    if (btn) {
      btn.addEventListener("click", () => {
        const next = _currencyMode() === "krw" ? "usd" : "krw";
        try { localStorage.setItem(CURRENCY_KEY, next); } catch (e) { /* 무시 */ }
        applyCurrencyMode(next);
        // ★ 폴링 주기를 기다리지 않고 지금 보이는 해외 금액을 바로 다시 그린다 - 대시보드는
        // 필터를 다시 적용하면 되고, 성과/매매일지는 onShow 를 그대로 다시 부르면 된다(둘 다
        // cacheMs 를 확인하는 showTab() 대신 handlers.onShow() 를 직접 호출해 캐시를 무시한다).
        if (_activeTab === "dash" && typeof _applyDashMarketFilter === "function") _applyDashMarketFilter();
        else if (_panels[_activeTab] && typeof _panels[_activeTab].onShow === "function") _panels[_activeTab].onShow();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
    document.addEventListener("DOMContentLoaded", initTheme);
    document.addEventListener("DOMContentLoaded", initCurrencyToggle);
  } else {
    boot();
    initTheme();
    initCurrencyToggle();
  }

  window.APP = {
    $, $$, el, won, signed, pct, pct0, eok, dir, api, toast, busy, fill, skeleton,
    NAV, registerPanel, showTab, focusCandidate,
  };
})();

