"use strict";
(function () {
  function CSS(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ━━ 숫자 애니메이션 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function animateNumber(el, from, to, opts) {
    opts = opts || {};
    const ms = opts.ms || 200;
    const fmt = opts.fmt || ((v) => String(Math.round(v)));

    if (REDUCED || from === to || !isFinite(from)) {
      el.textContent = fmt(to);
      return;
    }

    const dir = to > from ? "flash-up" : "flash-down";
    el.classList.add(dir);
    const start = performance.now();

    function step(now) {
      const t = Math.min(1, (now - start) / ms);
      el.textContent = fmt(from + (to - from) * t);
      if (t < 1) {
        requestAnimationFrame(step);
      } else {
        el.textContent = fmt(to);
        setTimeout(() => el.classList.remove("flash-up", "flash-down"), 300);
      }
    }
    requestAnimationFrame(step);
  }

  // ━━ 스파크라인 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function sparkline(canvas, points, opts) {
    opts = opts || {};
    const color = opts.color || CSS("--ink", "#1a1d21");
    const fill = opts.fill !== false;
    const height = opts.height || 26;
    const dpr = window.devicePixelRatio || 1;

    let pts = (points || []).slice();
    if (pts.length > 60) {
      const step = pts.length / 60;
      const sampled = [];
      for (let i = 0; i < 60; i++) sampled.push(pts[Math.floor(i * step)]);
      pts = sampled;
    }

    const rect = canvas.getBoundingClientRect();
    const w = Math.max(rect.width || canvas.width || 80, 40);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = height + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, height);
    if (pts.length < 2) return;

    let min = Math.min.apply(null, pts);
    let max = Math.max.apply(null, pts);
    if (min === max) {
      min -= 1;
      max += 1;
    }

    const xAt = (i) => (i / (pts.length - 1)) * w;
    const yAt = (v) => height - ((v - min) / (max - min)) * height;

    ctx.beginPath();
    pts.forEach((v, i) => {
      const x = xAt(i);
      const y = yAt(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.25;
    ctx.stroke();

    if (fill) {
      ctx.lineTo(w, height);
      ctx.lineTo(0, height);
      ctx.closePath();
      ctx.globalAlpha = 0.12;
      ctx.fillStyle = color;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }

  // ━━ 값 서식 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function GFMT(v, unit) {
    if (v == null || (typeof v === "number" && isNaN(v))) return "-";
    if (unit === "원") return Math.round(v).toLocaleString("ko-KR") + "원";
    if (unit === "%") return (v * 100).toFixed(2) + "%";
    // ★ 승률처럼 "수준"을 나타내는 값은 부호를 붙이면 잘못 읽힌다("+53%"는
    // 변화량처럼 보인다). pct0 는 부호 없이 정수 %로만 보여준다.
    if (unit === "%0" || unit === "pct0") return Math.round(v * 100) + "%";
    if (unit === "배") return Number(v).toFixed(2) + "배";
    if (unit === "봉" || unit === "개" || unit === "분") return Math.round(v) + unit;
    if (typeof v === "number") return v.toFixed(2);
    return String(v);
  }

  const fmtTermVal = GFMT;

  // ━━ RealtimeChart ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  const PALETTE_VARS = ["--series1", "--series2", "--series3", "--series4", "--series5", "--series6"];

  class RealtimeChart {
    constructor(container, opts) {
      opts = opts || {};
      this.el = container;
      this.height = opts.height || 220;
      this.yFormat = opts.yFormat || ((v) => Math.round(v).toLocaleString("ko-KR"));
      // ★ 눈금 라벨은 좁은 자리(우측 64px)에 들어가야 해서 툴팁용 yFormat 과
      //   따로 둔다(예: 눈금은 "+1.2만", 툴팁은 "+12,345원"). 제목은 단위를 알린다.
      this.yAxisFormat = opts.yAxisFormat || this.yFormat;
      this.yTitle = opts.yTitle || "";
      this.xTitle = opts.xTitle || "";
      this._spanMs = 0;
      this.zeroBase = !!opts.zeroBase;
      this.guides = opts.guides || null;
      this._winOpt = opts.window || 240;

      this.series = [];
      // ★ 색은 종목을 따라간다 - 순위가 아니라. 처음 본 순서로 자리를 박아 두고
      // 청산으로 종목이 빠져도 남은 종목의 색은 밀리지 않는다.
      this._colorMap = new Map();
      this._visible = new Set();
      this._markers = [];

      this._yZoom = 1; // 금액축 배율
      this._timeWindowPts = null; // 시간축 줌(실제 표시 점 수). null=기본(winOpt)
      this._prevRange = null; // y범위 보간용

      this._crosshairX = null;
      this._layout = null;
      this._stopped = false;

      this._buildDom();
      this._bindEvents();
      this._ro = new ResizeObserver(() => this._resize());
      this._ro.observe(this.el);
      this._resize();
    }

    _pad() {
      // ★ 경계값은 여기 한 곳에서만 정의하고 _zone()/draw() 가 똑같이 참조한다.
      // 다르면 라벨 위인데 본문으로 잡혀 둘 다 줌된다. b>=26 이어야 x축 글자가 안 잘린다.
      return { l: 8, r: 64, t: this.yTitle ? 26 : 14, b: this.xTitle ? 42 : 28 };
    }

    _buildDom() {
      this.el.classList.add("chartbox");
      this.el.style.position = "relative";

      this.canvas = document.createElement("canvas");
      this.canvas.style.display = "block";
      this.canvas.style.width = "100%";
      this.canvas.style.height = this.height + "px";
      this.el.appendChild(this.canvas);
      this.ctx = this.canvas.getContext("2d");

      this.tooltip = document.createElement("div");
      this.tooltip.className = "chart-tip";
      this.tooltip.style.display = "none";
      this.el.appendChild(this.tooltip);

      this.legend = document.createElement("div");
      this.legend.className = "legend";
      this.el.appendChild(this.legend);

      // 접근성: Canvas 옆에 시각적으로 숨긴 표를 둔다.
      this.a11yTable = document.createElement("table");
      this.a11yTable.className = "visually-hidden";
      this.el.appendChild(this.a11yTable);
    }

    _colorFor(id) {
      if (!this._colorMap.has(id)) this._colorMap.set(id, this._colorMap.size);
      const idx = this._colorMap.get(id);
      if (idx >= PALETTE_VARS.length) return CSS("--series-etc", "#6e7178");
      return CSS(PALETTE_VARS[idx], "#6e7178");
    }

    setSeries(series) {
      series = series || [];
      series.forEach((s) => this._colorFor(s.id)); // 자리를 먼저 박아 둔다.
      this.series = series;
      // ★ 처음 보는 선만 자동으로 켠다 - 사용자가 범례로 끈 선은 그대로 두되,
      //   장중에 새로 생긴 종목이 (예전 끄기 때문에) 계속 안 보이면 안 된다.
      this._seen = this._seen || new Set();
      series.forEach((s) => {
        if (!this._seen.has(s.id)) {
          this._seen.add(s.id);
          this._visible.add(s.id);
        }
      });
      this._updateA11yTable();
      this.draw();
    }

    setGuides(guides) {
      this.guides = guides;
      this.draw();
    }

    setWindow(n) {
      this._winOpt = n;
      this.draw();
    }

    push(seriesId, point) {
      const s = this.series.find((s) => s.id === seriesId);
      if (!s) return;
      s.points.push(point);
      this._updateA11yTable();
      this.draw();
    }

    addMarker(marker) {
      this._markers.push(marker);
      this.draw();
    }

    toggle(seriesId) {
      this._hiddenExplicitly = true;
      if (this._visible.has(seriesId)) this._visible.delete(seriesId);
      else this._visible.add(seriesId);
      this.draw();
    }

    destroy() {
      this._stopped = true;
      this._ro.disconnect();
      this.el.removeEventListener("wheel", this._onWheel);
      this.el.removeEventListener("mousemove", this._onMouseMove);
      this.el.removeEventListener("mouseleave", this._onMouseLeave);
      this.el.removeEventListener("dblclick", this._onDblClick);
      this.el.removeEventListener("touchstart", this._onTouchStart);
      this.el.removeEventListener("touchmove", this._onTouchMove);
      this.el.removeEventListener("touchend", this._onTouchEnd);
      this.el.innerHTML = "";
    }

    _resize() {
      const rect = this.el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(rect.width, 100);
      const h = this.height;
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this.canvas.style.width = w + "px";
      this.canvas.style.height = h + "px";
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this._cssWidth = w;
      this._cssHeight = h;
      this.draw();
    }

    _maxLen() {
      let m = 0;
      this.series.forEach((s) => {
        if (this._visible.has(s.id)) m = Math.max(m, s.points.length);
      });
      return m;
    }

    _winCount() {
      return this._winOpt;
    }

    _visiblePointCount(total) {
      // ★ 시간축 줌의 기준은 '창 설정'이 아니라 실제 보이는 점 수다.
      const cur = Math.min(this._timeWindowPts || this._winCount(), total);
      return Math.max(1, cur);
    }

    _zone(e) {
      const rect = this.el.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const PAD = this._pad();
      const w = this._cssWidth;
      const h = this._cssHeight;
      if (x >= w - PAD.r) return "y";
      if (y >= h - PAD.b) return "x";
      return "body";
    }

    _zoomTime(deltaY, total) {
      if (total < 3) return; // 최소 점수(3개) 조건은 시간축에만 건다.
      const curVisible = Math.min(this._timeWindowPts || this._winCount(), total);
      let next = Math.round(curVisible * (deltaY < 0 ? 0.8 : 1.25));
      next = Math.max(3, next);
      this._timeWindowPts = next >= total ? null : next;
    }

    _zoomY(deltaY) {
      // ★ 금액축 줌은 점 개수와 무관하다. 최소 점수 조건은 시간축에만 건다.
      const factor = deltaY < 0 ? 1.15 : 1 / 1.15;
      this._yZoom = Math.max(0.2, Math.min(this._yZoom * factor, 20));
    }

    _bindEvents() {
      this._onWheel = (e) => {
        // ★ preventDefault() 는 조기 return 앞에 둔다.
        e.preventDefault();
        e.stopPropagation();
        if (!this.series.length) return;

        const zone = this._zone(e);
        const total = this._maxLen();
        if (zone === "body" || zone === "x") this._zoomTime(e.deltaY, total);
        if (zone === "body" || zone === "y") this._zoomY(e.deltaY);
        this.draw();
      };
      this.el.addEventListener("wheel", this._onWheel, { passive: false });

      this._onMouseMove = (e) => {
        const rect = this.el.getBoundingClientRect();
        this._crosshairX = e.clientX - rect.left;
        this._showTooltip(this._crosshairX, e.clientY - rect.top);
        this.draw();
      };
      this.el.addEventListener("mousemove", this._onMouseMove);

      this._onMouseLeave = () => {
        this._crosshairX = null;
        this.tooltip.style.display = "none";
        this.draw();
      };
      this.el.addEventListener("mouseleave", this._onMouseLeave);

      this._onDblClick = () => {
        this._timeWindowPts = null;
        this._yZoom = 1;
        this.draw();
      };
      this.el.addEventListener("dblclick", this._onDblClick);

      // ★ 모바일: 두 손가락 핀치로 줌(벌리면 확대), 더블탭으로 초기화. 한 손가락 세로 스크롤은 그대로 둔다.
      this.canvas.style.touchAction = "pan-y";
      let pinch = null;
      let lastTap = 0;
      const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
      this._onTouchStart = (e) => {
        if (e.touches.length === 2) {
          pinch = { d: dist(e.touches) };
        } else if (e.touches.length === 1) {
          const now = Date.now();
          if (now - lastTap < 300) this._onDblClick();
          lastTap = now;
        }
      };
      this._onTouchMove = (e) => {
        if (e.touches.length !== 2 || !pinch) return;
        e.preventDefault();
        const d = dist(e.touches);
        const ratio = d / pinch.d;
        if (ratio > 1.12 || ratio < 0.89) {
          const dy = ratio > 1 ? -1 : 1;  // 벌리면 확대(wheel 위로 = 확대와 같은 방향)
          if (this.series.length) {
            this._zoomTime(dy, this._maxLen());
            this._zoomY(dy);
            this.draw();
          }
          pinch.d = d;
        }
      };
      this._onTouchEnd = (e) => { if (e.touches.length < 2) pinch = null; };
      this.el.addEventListener("touchstart", this._onTouchStart, { passive: true });
      this.el.addEventListener("touchmove", this._onTouchMove, { passive: false });
      this.el.addEventListener("touchend", this._onTouchEnd, { passive: true });
    }

    _fmtTime(t, full) {
      if (t == null || t === "") return "";
      // ★★★ 시각이 숫자(밀리초)로 오는 차트(해외·암호화폐·통합)는 예전에
      // String(t).slice(0,5) 로 잘려 "17587" 같은 엉뚱한 라벨이 찍혔다.
      if (typeof t === "number") {
        const d = new Date(t);
        if (isNaN(d.getTime())) return "";
        const p = (n) => String(n).padStart(2, "0");
        const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
        if (full) return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}:${p(d.getSeconds())}`;
        // 표시 구간이 하루를 넘으면 날짜를 함께 보여준다.
        return this._spanMs > 20 * 3600 * 1000 ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}` : hm;
      }
      const s = String(t);
      const iso = s.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/);
      if (iso) return full ? `${iso[1]} ${iso[2]}:${iso[3]}:${iso[4] || "00"}` : `${iso[2]}:${iso[3]}`;
      return s.slice(0, 5);
    }

    _esc(v) {
      return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    draw() {
      // ★ 탭이 안 보이면 렌더를 멈춘다 (배터리·CPU).
      if (document.hidden || this._stopped) return;

      const ctx = this.ctx;
      const w = this._cssWidth || this.el.clientWidth || 600;
      const h = this._cssHeight || this.height;
      ctx.clearRect(0, 0, w, h);

      const visibleSeries = this.series.filter((s) => this._visible.has(s.id));
      if (!visibleSeries.length) {
        ctx.fillStyle = CSS("--muted", "#6e7178");
        ctx.font = "13px sans-serif";
        ctx.fillText("표시할 데이터가 없습니다.", 12, h / 2);
        this._renderLegend();
        return;
      }

      const PAD = this._pad();
      const total = this._maxLen();
      const visibleCount = this._visiblePointCount(total);
      const trimmed = visibleSeries.map((s) => ({ ...s, pts: s.points.slice(-visibleCount) }));

      let rawMin = Infinity;
      let rawMax = -Infinity;
      trimmed.forEach((s) =>
        s.pts.forEach((p) => {
          rawMin = Math.min(rawMin, p.v);
          rawMax = Math.max(rawMax, p.v);
        })
      );
      if (this.zeroBase) rawMin = Math.min(rawMin, 0);
      if (this.guides) {
        if (this.guides.stop != null) rawMin = Math.min(rawMin, this.guides.stop);
        if (this.guides.target != null) rawMax = Math.max(rawMax, this.guides.target);
      }
      if (!isFinite(rawMin) || !isFinite(rawMax)) return;
      if (rawMin === rawMax) {
        rawMin -= 1;
        rawMax += 1;
      }

      // ★ Y축 범위를 k=0.35 로 보간해 점프를 막는다.
      if (this._prevRange) {
        rawMin = this._prevRange.min + (rawMin - this._prevRange.min) * 0.35;
        rawMax = this._prevRange.max + (rawMax - this._prevRange.max) * 0.35;
      }
      this._prevRange = { min: rawMin, max: rawMax };

      // ★ 금액축 줌은 자동 맞춤이 끝난 뒤에 적용한다. 먼저 적용하면 다음
      // 프레임의 자동 맞춤이 지워 버린다.
      const mid = (rawMin + rawMax) / 2;
      const half = ((rawMax - rawMin) / 2) / this._yZoom;
      const min = mid - half;
      const max = mid + half;

      const graphW = w - PAD.l - PAD.r;
      const graphH = h - PAD.t - PAD.b;
      const maxN = Math.max.apply(null, trimmed.map((s) => s.pts.length).concat([1]));

      const xAt = (i) => PAD.l + (maxN <= 1 ? graphW / 2 : (i / (maxN - 1)) * graphW);
      const yAt = (v) => PAD.t + ((max - v) / (max - min)) * graphH;

      if (this.zeroBase) {
        const y0 = yAt(0);
        // ★ 이익 영역(0선 위)은 빨강, 손실 영역(0선 아래)은 파랑 - 손익 색 규칙과 같다.
        ctx.fillStyle = "rgba(192,57,43,0.06)";
        ctx.fillRect(PAD.l, PAD.t, graphW, Math.max(0, Math.min(y0, PAD.t + graphH) - PAD.t));
        ctx.fillStyle = "rgba(20,83,154,0.06)";
        ctx.fillRect(PAD.l, Math.max(PAD.t, y0), graphW, Math.max(0, PAD.t + graphH - Math.max(PAD.t, y0)));
      }

      // y축 눈금: 가로 격자 4단 + 우측 값.
      ctx.strokeStyle = CSS("--rule2", "#eceae4");
      ctx.fillStyle = CSS("--muted", "#6e7178");
      ctx.font = "10px sans-serif";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const v = min + ((max - min) * i) / 4;
        const y = yAt(v);
        ctx.beginPath();
        ctx.moveTo(PAD.l, y);
        ctx.lineTo(w - PAD.r, y);
        ctx.stroke();
        ctx.fillText(this.yAxisFormat(v), w - PAD.r + 4, y + 3);
      }

      // ★ 축 제목 - 눈금 숫자만 있으면 무슨 값·단위인지 알 수 없다.
      if (this.yTitle) ctx.fillText(this.yTitle, w - PAD.r + 4, 12);
      if (this.xTitle) {
        const tw = ctx.measureText(this.xTitle).width;
        ctx.fillText(this.xTitle, PAD.l + graphW / 2 - tw / 2, h - 5);
      }

      // x축 라벨: 최대 6개. 표시 구간(시각이 숫자일 때만 의미)을 먼저 구해
      // 하루를 넘으면 날짜를 붙인다.
      let tMin = Infinity;
      let tMax = -Infinity;
      trimmed.forEach((s) => s.pts.forEach((p) => {
        if (typeof p.t === "number") { tMin = Math.min(tMin, p.t); tMax = Math.max(tMax, p.t); }
      }));
      this._spanMs = isFinite(tMin) ? tMax - tMin : 0;
      const tickCount = Math.min(6, maxN);
      let prevLabel = null;
      for (let k = 0; k < tickCount; k++) {
        const idx = Math.round((k * (maxN - 1)) / Math.max(tickCount - 1, 1));
        const withPoint = trimmed.find((s) => s.pts[idx]);
        if (!withPoint) continue;
        const label = this._fmtTime(withPoint.pts[idx].t);
        if (!label || label === prevLabel) continue;
        prevLabel = label;
        // ★ 라벨을 눈금 위치에 가운데 맞추고 그래프 밖으로 안 나가게 가둔다
        //   (예전엔 x-14 고정이라 양 끝 라벨이 잘리거나 어긋났다).
        const tw = ctx.measureText(label).width;
        const lx = Math.max(PAD.l, Math.min(xAt(idx) - tw / 2, w - PAD.r - tw));
        ctx.fillText(label, lx, h - PAD.b + 14);
      }

      // 가이드(손절·목표).
      if (this.guides) {
        ctx.setLineDash([4, 4]);
        if (this.guides.stop != null) {
          const y = yAt(this.guides.stop);
          ctx.strokeStyle = CSS("--fall", "#14539a");
          ctx.beginPath();
          ctx.moveTo(PAD.l, y);
          ctx.lineTo(w - PAD.r, y);
          ctx.stroke();
          ctx.fillStyle = CSS("--fall", "#14539a");
          ctx.fillText("손절", w - PAD.r + 4, y - 3);
        }
        if (this.guides.target != null) {
          const y = yAt(this.guides.target);
          ctx.strokeStyle = CSS("--rise", "#c0392b");
          ctx.beginPath();
          ctx.moveTo(PAD.l, y);
          ctx.lineTo(w - PAD.r, y);
          ctx.stroke();
          ctx.fillStyle = CSS("--rise", "#c0392b");
          ctx.fillText("목표", w - PAD.r + 4, y - 3);
        }
        ctx.setLineDash([]);
      }

      // 계열 선. ★ 종목선은 부호가 아니라 종목으로 칠한다.
      trimmed.forEach((s) => {
        const color = s.color || this._colorFor(s.id);
        if (!s.pts.length) return;
        ctx.beginPath();
        s.pts.forEach((p, i) => {
          const x = xAt(i);
          const y = yAt(p.v);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = color;
        ctx.lineWidth = s.bold ? 2.5 : 1.25;
        if (s.dashed) {
          ctx.setLineDash([5, 4]);
          ctx.globalAlpha = 0.6;
        }
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;

        // ★ 색만으로 구분하게 두지 않는다 - 선 끝에 종목 이름을 9px 로 적는다.
        const last = s.pts[s.pts.length - 1];
        const lx = xAt(s.pts.length - 1);
        const ly = yAt(last.v);
        ctx.fillStyle = color;
        ctx.font = "9px sans-serif";
        ctx.fillText(s.name || s.id, Math.min(lx + 4, w - PAD.r - 2), ly);
      });

      // 마커: 진입▲ 청산▼.
      this._markers.forEach((m) => {
        const s = trimmed.find((s) => s.id === m.seriesId) || trimmed[0];
        if (!s) return;
        const idx = s.pts.findIndex((p) => p.t === m.t);
        if (idx < 0) return;
        const x = xAt(idx);
        const y = yAt(m.v != null ? m.v : s.pts[idx].v);
        const isBuy = m.kind === "buy";
        ctx.fillStyle = isBuy ? CSS("--rise", "#c0392b") : CSS("--fall", "#14539a");
        ctx.beginPath();
        if (isBuy) {
          ctx.moveTo(x - 4, y + 6);
          ctx.lineTo(x + 4, y + 6);
          ctx.lineTo(x, y - 2);
        } else {
          ctx.moveTo(x - 4, y - 6);
          ctx.lineTo(x + 4, y - 6);
          ctx.lineTo(x, y + 2);
        }
        ctx.closePath();
        ctx.fill();
      });

      // 크로스헤어.
      if (this._crosshairX != null && this._crosshairX >= PAD.l && this._crosshairX <= w - PAD.r) {
        ctx.strokeStyle = CSS("--faint", "#9b9da2");
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(this._crosshairX, PAD.t);
        ctx.lineTo(this._crosshairX, PAD.t + graphH);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // 확대 안내 - 안 적으면 데이터가 사라진 줄 안다.
      if (this._timeWindowPts || this._yZoom !== 1) {
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.font = "11px sans-serif";
        ctx.fillText(
          `시간 ${visibleCount}점 · 금액 ×${this._yZoom.toFixed(1)} · 두 번 누르면 전체`,
          PAD.l,
          PAD.t - 3
        );
      }

      this._layout = { PAD, w, h, xAt, yAt, min, max, trimmed, maxN };
      this._renderLegend();
    }

    _renderLegend() {
      this.legend.innerHTML = "";
      this.series.forEach((s) => {
        const item = document.createElement("span");
        item.style.cursor = "pointer";
        item.style.opacity = this._visible.has(s.id) ? "1" : "0.4";
        const dot = document.createElement("i");
        dot.className = "dot";
        dot.style.background = s.color || this._colorFor(s.id);
        item.appendChild(dot);
        item.appendChild(document.createTextNode(s.name || s.id));
        item.addEventListener("click", () => this.toggle(s.id));
        this.legend.appendChild(item);
      });
    }

    _showTooltip(x, y) {
      const layout = this._layout;
      if (!layout || !layout.trimmed.length) {
        this.tooltip.style.display = "none";
        return;
      }
      const { PAD, maxN, trimmed, w, h } = layout;
      if (x < PAD.l || x > w - PAD.r) {
        this.tooltip.style.display = "none";
        return;
      }
      const graphW = w - PAD.l - PAD.r;
      let i = Math.round(((x - PAD.l) / graphW) * (maxN - 1));
      i = Math.max(0, Math.min(maxN - 1, i));

      let html = "";
      let anyTime = "";
      trimmed.forEach((s) => {
        const p = s.pts[i];
        if (!p) return;
        anyTime = anyTime || p.t;
        const color = s.color || this._colorFor(s.id);
        html +=
          `<div><i class="dot" style="background:${color};display:inline-block;width:8px;height:8px;` +
          `border-radius:50%;margin-right:4px;"></i>${this._esc(s.name || s.id)}: ${this.yFormat(p.v)}</div>`;
      });
      if (!html) {
        this.tooltip.style.display = "none";
        return;
      }
      html = `<div>${this._fmtTime(anyTime, true)}</div>` + html;

      this._markers
        .filter((m) => {
          const s = trimmed.find((s) => s.id === m.seriesId) || trimmed[0];
          const idx = s ? s.pts.findIndex((p) => p.t === m.t) : -1;
          return idx === i;
        })
        .forEach((m) => {
          html += `<div>${m.kind === "buy" ? "매수" : "매도"} ${this.yFormat(m.v)}</div>`;
        });

      this.tooltip.innerHTML = html;
      this.tooltip.style.display = "block";

      // ★ 화면 밖으로 안 나가게 뒤집는다.
      let left = x + 12;
      if (left + 170 > w) left = x - 182;
      let top = y - 10;
      if (top + 90 > h) top = y - 100;
      this.tooltip.style.left = Math.max(0, left) + "px";
      this.tooltip.style.top = Math.max(0, top) + "px";
    }

    _updateA11yTable() {
      const maxN = Math.max.apply(null, this.series.map((s) => s.points.length).concat([0]));
      let html = "<caption>차트 데이터</caption><thead><tr><th>시각</th>";
      this.series.forEach((s) => (html += `<th>${s.name || s.id}</th>`));
      html += "</tr></thead><tbody>";
      for (let i = 0; i < maxN; i++) {
        const anyPt = this.series.map((s) => s.points[i]).find(Boolean);
        html += `<tr><td>${anyPt ? this._fmtTime(anyPt.t) : ""}</td>`;
        this.series.forEach((s) => {
          const p = s.points[i];
          html += `<td>${p ? this.yFormat(p.v) : ""}</td>`;
        });
        html += "</tr>";
      }
      html += "</tbody>";
      this.a11yTable.innerHTML = html;
    }
  }

  // ━━ DataGrid ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  class DataGrid {
    constructor(container, opts) {
      opts = opts || {};
      this.el = container;
      this.columns = opts.columns || [];
      this.rows = opts.rows || [];
      this.expand = opts.expand || null;
      this.showFilters = opts.filters === true;  // 열 아래 필터 입력칸(작은 네모) - 보유 몇 줄짜리 표에는 필요 없다
      this.groupBy = opts.groupBy || null;
      this.page = opts.page != null ? opts.page : 50;
      this.storageKey = opts.storageKey || null;
      this.toolbarExtra = opts.toolbarExtra || null;  // ★ 날짜 선택 등 외부 요소를 검색·CSV와 같은 줄에 배치

      this.sortState = [];
      this.filters = {};
      this.search = "";
      this.currentPage = 0;
      this._expanded = new Set();

      this._loadState();
      this.render();
    }

    _loadState() {
      if (!this.storageKey) return;
      try {
        const raw = localStorage.getItem(this.storageKey);
        if (!raw) return;
        const st = JSON.parse(raw);
        this.sortState = st.sortState || [];
        if (st.columnOrder) {
          const map = new Map(this.columns.map((c) => [c.key, c]));
          const ordered = st.columnOrder.map((k) => map.get(k)).filter(Boolean);
          this.columns.forEach((c) => {
            if (!st.columnOrder.includes(c.key)) ordered.push(c);
          });
          this.columns = ordered;
        }
      } catch (e) {
        /* 저장된 상태가 깨졌으면 조용히 무시하고 기본값을 쓴다. */
      }
    }

    _saveState() {
      if (!this.storageKey) return;
      try {
        localStorage.setItem(
          this.storageKey,
          JSON.stringify({ sortState: this.sortState, columnOrder: this.columns.map((c) => c.key) })
        );
      } catch (e) {
        /* 저장 공간이 없거나 막혀 있으면 그냥 넘어간다. */
      }
    }

    setRows(rows) {
      this.rows = rows || [];
      this.currentPage = 0;
      this.render();
    }

    _matchFilter(value, expr) {
      if (expr == null || expr === "") return true;
      expr = String(expr).trim();

      const range = expr.match(/^(-?[\d.]+)\.\.(-?[\d.]+)$/);
      if (range) {
        const v = parseFloat(value);
        return !isNaN(v) && v >= parseFloat(range[1]) && v <= parseFloat(range[2]);
      }
      const cmp = expr.match(/^(>=|<=|>|<)\s*(-?[\d.]+)$/);
      if (cmp) {
        const v = parseFloat(value);
        if (isNaN(v)) return false;
        const n = parseFloat(cmp[2]);
        if (cmp[1] === ">") return v > n;
        if (cmp[1] === "<") return v < n;
        if (cmp[1] === ">=") return v >= n;
        return v <= n;
      }
      return String(value == null ? "" : value).toLowerCase().includes(expr.toLowerCase());
    }

    _filteredRows() {
      let rows = this.rows.filter((r) => {
        for (const col of this.columns) {
          const f = this.filters[col.key];
          if (f && !this._matchFilter(r[col.key], f)) return false;
        }
        if (this.search) {
          const hay = this.columns.map((c) => String(r[c.key] == null ? "" : r[c.key])).join(" ").toLowerCase();
          if (!hay.includes(this.search.toLowerCase())) return false;
        }
        return true;
      });

      if (this.sortState.length) {
        rows = rows.slice().sort((a, b) => {
          for (const s of this.sortState) {
            const av = a[s.key];
            const bv = b[s.key];
            let cmp;
            if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
            else cmp = String(av == null ? "" : av).localeCompare(String(bv == null ? "" : bv));
            if (cmp !== 0) return s.dir === "desc" ? -cmp : cmp;
          }
          return 0;
        });
      }
      return rows;
    }

    toggleSort(key, additive) {
      const idx = this.sortState.findIndex((s) => s.key === key);
      if (!additive) {
        if (idx === 0 && this.sortState.length === 1) {
          this.sortState[0].dir = this.sortState[0].dir === "asc" ? "desc" : "asc";
        } else {
          this.sortState = [{ key, dir: "asc" }];
        }
      } else if (idx >= 0) {
        this.sortState[idx].dir = this.sortState[idx].dir === "asc" ? "desc" : "asc";
      } else {
        this.sortState.push({ key, dir: "asc" });
      }
      this._saveState();
      this.currentPage = 0;
      this.render();
    }

    _aggregate(rows, col) {
      if (!col.agg) return null;
      const vals = rows.map((r) => Number(r[col.key])).filter((v) => !isNaN(v));
      if (col.agg === "sum") return vals.reduce((a, b) => a + b, 0);
      if (col.agg === "avg") return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
      if (col.agg === "count") return vals.length;
      if (col.agg === "min") return vals.length ? Math.min.apply(null, vals) : 0;
      if (col.agg === "max") return vals.length ? Math.max.apply(null, vals) : 0;
      return null;
    }

    exportCsv() {
      const rows = this._filteredRows();
      const header = this.columns.map((c) => c.label || c.key).join(",");
      const lines = rows.map((r) =>
        this.columns
          .map((c) => {
            let v = r[c.key];
            if (v == null) v = "";
            v = String(v).replace(/"/g, '""');
            return `"${v}"`;
          })
          .join(",")
      );
      // ★ UTF-8 BOM 을 붙인다 - 안 붙이면 엑셀에서 한글이 깨진다.
      const csv = "\uFEFF" + [header].concat(lines).join("\r\n");
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = (this.storageKey || "data") + ".csv";
      a.click();
      URL.revokeObjectURL(url);
    }

    render() {
      this.el.innerHTML = "";
      const allRows = this._filteredRows();

      const toolbar = document.createElement("div");
      toolbar.style.display = "flex";
      toolbar.style.gap = "8px";
      toolbar.style.marginBottom = "8px";
      toolbar.style.flexWrap = "wrap";
      toolbar.style.alignItems = "center";

      if (this.toolbarExtra) {
        toolbar.appendChild(this.toolbarExtra);
      }

      const searchBox = document.createElement("input");
      searchBox.type = "search";
      searchBox.placeholder = "검색...";
      searchBox.value = this.search;
      searchBox.style.flex = "1 1 120px";
      searchBox.style.minWidth = "0";
      searchBox.addEventListener("input", (e) => {
        this.search = e.target.value;
        this.currentPage = 0;
        this.render();
      });
      toolbar.appendChild(searchBox);

      const exportBtn = document.createElement("button");
      exportBtn.className = "b ghost small";
      exportBtn.textContent = "CSV 내보내기";
      exportBtn.addEventListener("click", () => this.exportCsv());
      toolbar.appendChild(exportBtn);

      this.el.appendChild(toolbar);

      if (!allRows.length) {
        const empty = document.createElement("div");
        empty.className = "banner";
        empty.textContent = "조건에 맞는 항목이 없습니다. 필터를 지워보세요.";
        const resetBtn = document.createElement("button");
        resetBtn.className = "b ghost small";
        resetBtn.style.marginTop = "8px";
        resetBtn.textContent = "필터 초기화";
        resetBtn.addEventListener("click", () => {
          this.filters = {};
          this.search = "";
          this.render();
        });
        empty.appendChild(document.createElement("br"));
        empty.appendChild(resetBtn);
        this.el.appendChild(empty);
        return;
      }

      const table = document.createElement("table");
      table.className = "grid";
      table.setAttribute("role", "grid");

      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      this.columns.forEach((col) => {
        const th = document.createElement("th");
        // ★★★ 어수선함의 원인 하나 - 예전엔 모든 셀이 오른쪽 정렬이라
        // 종목명·사유 같은 글자 컬럼까지 오른쪽으로 밀려 들쭉날쭉했다.
        // 숫자는 오른쪽(자릿수가 맞아야 비교된다), 글자는 왼쪽이 맞다.
        if (col.numeric) th.classList.add("num");
        th.textContent = col.label || col.key;
        th.style.cursor = "pointer";
        const sIdx = this.sortState.findIndex((s) => s.key === col.key);
        if (sIdx >= 0) {
          th.setAttribute("aria-sort", this.sortState[sIdx].dir === "asc" ? "ascending" : "descending");
          const badge = document.createElement("sup");
          badge.textContent = " " + (sIdx + 1) + (this.sortState[sIdx].dir === "asc" ? "▲" : "▼");
          th.appendChild(badge);
        }
        th.addEventListener("click", (e) => this.toggleSort(col.key, e.shiftKey));
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);

      const filterRow = document.createElement("tr");
      filterRow.className = "filter-row";
      this.columns.forEach((col) => {
        const td = document.createElement("td");
        const input = document.createElement("input");
        input.value = this.filters[col.key] || "";
        input.placeholder = col.numeric ? ">100 · 10..99" : "필터";
        input.addEventListener("input", (e) => {
          this.filters[col.key] = e.target.value;
          this.currentPage = 0;
          this.render();
        });
        td.appendChild(input);
        filterRow.appendChild(td);
      });
      if (this.showFilters) thead.appendChild(filterRow);
      table.appendChild(thead);

      const tbody = document.createElement("tbody");
      const pageRows = this.page
        ? allRows.slice(this.currentPage * this.page, (this.currentPage + 1) * this.page)
        : allRows;

      const maxByCol = {};
      this.columns.forEach((c) => {
        if (c.bar) {
          const vals = allRows.map((r) => Number(r[c.key])).filter((v) => !isNaN(v));
          maxByCol[c.key] = vals.length ? Math.max.apply(null, vals.map(Math.abs)) : 1;
        }
      });

      pageRows.forEach((row) => {
        const tr = document.createElement("tr");
        this.columns.forEach((col) => {
          const td = document.createElement("td");
          if (col.numeric) td.classList.add("num");
          const val = row[col.key];
          // ★ 손익 색 규칙(국내 관례): 이익 = 빨강(rise), 손실 = 파랑(fall), 0 은 무색.
          const signCls = typeof val === "number" ? (val > 0 ? "rise" : val < 0 ? "fall" : "") : "";
          if (col.bar) {
            td.classList.add("bar-cell");
            const fill = document.createElement("div");
            fill.className = "bar-fill " + signCls;
            const pct = maxByCol[col.key] ? Math.min(100, (Math.abs(val) / maxByCol[col.key]) * 100) : 0;
            fill.style.width = pct + "%";
            td.appendChild(fill);
            const span = document.createElement("span");
            span.className = signCls;
            // ★★ A-24: fmt() 가 techBadgeHTML() 처럼 HTML 문자열을 낼 수 있다.
            // textContent 로 넣으면 태그가 그대로 글자로 보인다 - innerHTML 로 넣는다.
            // (fmt 이 없을 때의 원시 val 은 신뢰할 수 없으니 textContent 로 이스케이프한다.)
            if (col.fmt) span.innerHTML = col.fmt(val, row);
            else span.textContent = val;
            td.appendChild(span);
          } else if (col.tone === "pnl") {
            // 막대 없이 숫자만 색을 입히는 손익 열(누적·MDD·수익금액 등).
            const span = document.createElement("span");
            span.className = signCls;
            span.innerHTML = col.fmt ? col.fmt(val, row) : (val == null ? "" : String(val));
            td.appendChild(span);
          } else if (col.fmt) {
            td.innerHTML = col.fmt(val, row);
          } else {
            td.textContent = val == null ? "" : val;
          }
          tr.appendChild(td);
        });

        if (this.expand) {
          tr.style.cursor = "pointer";
          tr.setAttribute("aria-expanded", this._expanded.has(row) ? "true" : "false");
          tr.addEventListener("click", () => {
            if (this._expanded.has(row)) this._expanded.delete(row);
            else this._expanded.add(row);
            this.render();
          });
        }
        tbody.appendChild(tr);

        if (this.expand && this._expanded.has(row)) {
          const exTr = document.createElement("tr");
          exTr.className = "expand-row";
          const exTd = document.createElement("td");
          exTd.colSpan = this.columns.length;
          const content = this.expand(row);
          if (content instanceof HTMLElement) exTd.appendChild(content);
          else exTd.innerHTML = content;
          exTr.appendChild(exTd);
          tbody.appendChild(exTr);
        }
      });
      table.appendChild(tbody);

      if (this.columns.some((c) => c.agg)) {
        const tfoot = document.createElement("tfoot");
        const sumTr = document.createElement("tr");
        sumTr.className = "summary-row";
        this.columns.forEach((col, i) => {
          const td = document.createElement("td");
          if (col.agg) {
            const v = this._aggregate(allRows, col); // 필터가 걸리면 필터된 것만 집계한다.
            td.textContent = col.fmt ? col.fmt(v) : v;
          } else if (i === 0) {
            td.textContent = `합계 (${allRows.length}건)`;
          }
          sumTr.appendChild(td);
        });
        tfoot.appendChild(sumTr);
        table.appendChild(tfoot);
      }

      // ★★★ 실제로 겪은 버그 - 테이블을 감싸는 스크롤 컨테이너가 없어서,
      // 컬럼이 많거나(예: "전체 시장 종목" 시트) 값이 길면(모드 배지+긴
      // 종목명 등) 테이블이 부모보다 넓어지고 화면이 우측으로 흘러넘쳤다.
      // 넘치는 만큼은 가로 스크롤로 흡수하고, 화면 자체는 안 커지게 한다.
      const tableWrap = document.createElement("div");
      tableWrap.style.overflowX = "auto";
      tableWrap.style.maxWidth = "100%";
      tableWrap.appendChild(table);
      this.el.appendChild(tableWrap);

      if (this.page) {
        const totalPages = Math.ceil(allRows.length / this.page);
        if (totalPages > 1) {
          const pager = document.createElement("div");
          pager.style.marginTop = "8px";
          pager.style.display = "flex";
          pager.style.gap = "4px";
          for (let p = 0; p < totalPages; p++) {
            const btn = document.createElement("button");
            btn.className = "b small ghost" + (p === this.currentPage ? " active" : "");
            btn.textContent = String(p + 1);
            btn.addEventListener("click", () => {
              this.currentPage = p;
              this.render();
            });
            pager.appendChild(btn);
          }
          this.el.appendChild(pager);
        }
      }
    }
  }

  // ━━ 판정 표시 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function conditionBar(verdict, opts) {
    opts = opts || {};
    const wrap = document.createElement("div");
    wrap.className = "verdict-list";
    const required = (verdict.terms || []).filter((t) => t.required);
    const passedCount = required.filter((t) => t.passed).length;

    if (opts.showTitle !== false) {
      const title = document.createElement("div");
      title.className = "verdict-title";
      title.textContent = `${verdict.technique_label || ""} · ${passedCount}/${required.length} 충족`;
      wrap.appendChild(title);
    }

    required.forEach((t) => {
      const row = document.createElement("div");
      row.className = "verdict-item " + (t.passed ? "pass" : "fail") + (t.flipped ? " flip" : "");
      const mark = document.createElement("span");
      mark.className = "verdict-mark";
      mark.textContent = t.passed ? "✓" : "✗";
      const label = document.createElement("span");
      label.className = "verdict-label";
      label.textContent = t.label;
      const value = document.createElement("span");
      value.className = "verdict-value";
      value.textContent = `${fmtTermVal(t.value, t.unit)} (기준 ${fmtTermVal(t.threshold, t.unit)})`;
      row.appendChild(mark);
      row.appendChild(label);
      row.appendChild(value);
      wrap.appendChild(row);
    });

    return wrap;
  }

  function evidencePanel(verdict) {
    const el = document.createElement("div");
    el.className = "evi";

    const title = document.createElement("div");
    title.className = "evi-title";
    title.textContent = `${verdict.technique_label || ""} · ${verdict.ok ? "통과" : "미통과"} · ${verdict.at || ""}`;
    el.appendChild(title);

    if (verdict.narrative) {
      const p = document.createElement("div");
      p.textContent = verdict.narrative;
      el.appendChild(p);
    }

    const table = document.createElement("table");
    table.className = "grid";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>항목</th><th>값</th><th>기준</th><th>판정</th><th>변화</th></tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    (verdict.terms || []).forEach((t) => {
      const tr = document.createElement("tr");
      if (t.required && !t.passed) tr.style.background = "rgba(20,83,154,0.08)";
      const cells = [
        t.label,
        fmtTermVal(t.value, t.unit),
        fmtTermVal(t.threshold, t.unit),
        (t.passed ? "통과" : "미통과") + (t.flipped ? " ⚡" : ""),
        t.delta != null ? fmtTermVal(t.delta, t.unit) : "",
      ];
      cells.forEach((c) => {
        const td = document.createElement("td");
        td.textContent = c;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    el.appendChild(table);

    if (verdict.changes && verdict.changes.length) {
      const h = document.createElement("div");
      h.textContent = "무엇이 바뀌었나";
      h.style.fontWeight = "600";
      h.style.marginTop = "8px";
      el.appendChild(h);
      const ul = document.createElement("ul");
      verdict.changes.forEach((c) => {
        const li = document.createElement("li");
        li.textContent = c;
        ul.appendChild(li);
      });
      el.appendChild(ul);
    }

    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "원자료 보기";
    details.appendChild(summary);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(verdict.inputs || {}, null, 2);
    details.appendChild(pre);
    el.appendChild(details);

    return el;
  }

  // ★★ 클래스를 전역에 두지 않고 window.UI 하나로만 내보낸다. 이름이
  // 겹치면 app.js 의 구조분해가 SyntaxError 로 죽어 화면은 뼈대만 뜨고
  // 콘솔에만 오류가 남아 원인을 찾기 어렵다.
  window.UI = { RealtimeChart, DataGrid, sparkline, animateNumber, conditionBar, evidencePanel, GFMT, fmtTermVal };
})();
