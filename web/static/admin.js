// 새 기기 승인 관리자 페이지(daytrader/server.py 의 /admin, _ADMIN_HTML 이 이 파일을 불러 쓴다).
// ★ 이 페이지는 비밀번호가 아니라 "서버 PC에서 연 브라우저"라는 사실로만 지켜진다(server.py 의
//   _is_admin_local). 그래서 CSP(script-src 'self')를 그대로 지키려고 인라인 스크립트 대신
//   이 파일로 분리했다.
// ★★ 보안 - ip·User-Agent 는 로그인을 시도한 쪽이 마음대로 채울 수 있는 값이다(User-Agent 헤더,
//   프록시를 흉내 낸 X-Forwarded-For). innerHTML 로 그대로 끼워 넣으면 저장형 XSS 가 되어, 관리자가
//   이 페이지를 여는 순간 공격자 스크립트가 관리자 권한(승인·해제)으로 실행된다. 그래서 반드시
//   textContent 로만 넣는다 - 절대 innerHTML 에 ip/ua 를 문자열로 이어 붙이지 않는다.

(function () {
  // ★ 트레이가 이 페이지를 열 때 서버가 <meta name="admin-token"> 에 실어 준 값 - 모든 /api/admin/*
  //   호출에 헤더로 실어 보낸다(서버가 같은 값인지 다시 확인한다). 트레이 없이(개발용) 띄웠으면 없다.
  const meta = document.querySelector('meta[name="admin-token"]');
  const ADMIN_TOKEN = meta ? meta.content : "";

  async function call(path, body) {
    const headers = body ? { "content-type": "application/json" } : {};
    if (ADMIN_TOKEN) headers["X-Admin-Token"] = ADMIN_TOKEN;
    const res = await fetch(path, {
      method: body ? "POST" : "GET",
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || ("HTTP " + res.status));
    }
    return res.json();
  }

  function fmt(ts) {
    return ts ? new Date(ts * 1000).toLocaleString("ko-KR") : "-";
  }

  function cell(text, cls) {
    const td = document.createElement("td");
    td.textContent = text;  // ★ innerHTML 아님 - ip/ua/시각을 안전하게 넣는다.
    if (cls) td.className = cls;
    return td;
  }

  function emptyRow(container, text) {
    container.innerHTML = "";  // 여긴 우리가 직접 쓴 고정 문구뿐이라 안전하다.
    const div = document.createElement("div");
    div.className = "empty";
    div.textContent = text;
    container.appendChild(div);
  }

  function table(headers) {
    const t = document.createElement("table");
    const head = document.createElement("tr");
    headers.forEach((h) => {
      const th = document.createElement("th");
      th.textContent = h;
      head.appendChild(th);
    });
    t.appendChild(head);
    return t;
  }

  function renderPending(rows) {
    const box = document.getElementById("pending");
    const waiting = rows.filter((r) => r.status === "pending");
    if (!waiting.length) {
      emptyRow(box, "대기 중인 요청이 없습니다.");
      return waiting.length;
    }
    box.innerHTML = "";
    const t = table(["요청 시각", "IP", "기기(User-Agent)", ""]);
    waiting.forEach((r) => {
      const tr = document.createElement("tr");
      tr.appendChild(cell(fmt(r.requested_at)));
      tr.appendChild(cell(r.ip, "mono"));
      tr.appendChild(cell((r.ua || "-").slice(0, 80), "mono"));
      const actions = document.createElement("td");
      const okBtn = document.createElement("button");
      okBtn.className = "ok";
      okBtn.textContent = "승인";
      okBtn.onclick = () => call("/api/admin/approve", { token: r.token }).then(refresh).catch(showError);
      const noBtn = document.createElement("button");
      noBtn.className = "no";
      noBtn.textContent = "거부";
      noBtn.onclick = () => call("/api/admin/deny", { token: r.token }).then(refresh).catch(showError);
      actions.append(okBtn, noBtn);
      tr.appendChild(actions);
      t.appendChild(tr);
    });
    box.appendChild(t);
    return waiting.length;
  }

  function renderTrusted(rows) {
    const box = document.getElementById("trusted");
    if (!rows.length) {
      emptyRow(box, "승인된 기기가 없습니다.");
      return 0;
    }
    box.innerHTML = "";
    const t = table(["승인 시각", "마지막 접속", "IP", "기기(User-Agent)", ""]);
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.appendChild(cell(fmt(r.approved_at)));
      tr.appendChild(cell(fmt(r.last_seen)));
      tr.appendChild(cell(r.ip, "mono"));
      tr.appendChild(cell((r.ua || "-").slice(0, 80), "mono"));
      const actions = document.createElement("td");
      const btn = document.createElement("button");
      btn.className = "no";
      btn.textContent = "해제";
      btn.onclick = () => {
        if (confirm("이 기기는 다음 접속부터 다시 승인이 필요합니다. 해제할까요?")) {
          call("/api/admin/revoke", { key: r.key }).then(refresh).catch(showError);
        }
      };
      actions.appendChild(btn);
      tr.appendChild(actions);
      t.appendChild(tr);
    });
    box.appendChild(t);
    return rows.length;
  }

  async function refresh() {
    const btn = document.getElementById("refresh-btn");
    if (btn) btn.classList.add("spin");
    try {
      const [{ pending }, { trusted }] = await Promise.all([call("/api/admin/pending"), call("/api/admin/trusted")]);
      const pendingCount = renderPending(pending);
      const trustedCount = renderTrusted(trusted);
      document.getElementById("pending-count").textContent = pendingCount ? `(${pendingCount})` : "";
      document.getElementById("trusted-count").textContent = trustedCount ? `(${trustedCount})` : "";
      const box = document.getElementById("admin-error");
      if (box) box.style.display = "none";
    } catch (e) {
      showError(e);
    } finally {
      if (btn) btn.classList.remove("spin");
    }
  }

  function showError(e) {
    const box = document.getElementById("admin-error");
    if (box) {
      box.textContent = "불러오기 실패: " + (e && e.message ? e.message : e);
      box.style.display = "";
    }
  }

  function selectTab(name) {
    document.getElementById("tab-pending").classList.toggle("active", name === "pending");
    document.getElementById("tab-trusted").classList.toggle("active", name === "trusted");
    document.getElementById("pending-panel").style.display = name === "pending" ? "" : "none";
    document.getElementById("trusted-panel").style.display = name === "trusted" ? "" : "none";
  }

  document.getElementById("tab-pending").onclick = () => selectTab("pending");
  document.getElementById("tab-trusted").onclick = () => selectTab("trusted");
  document.getElementById("refresh-btn").onclick = refresh;

  refresh();
  setInterval(refresh, 4000);
})();
