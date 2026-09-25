(function () {
  "use strict";

  // "언제 사서 언제 팔았는지"를 봉 위에 그대로 얹는다.
  // 보유 카드와 거래 기록이 같은 부품(CandleChart/tradeChart)을 쓴다.

  class CandleChart {
    constructor(container, opts) {
      opts = opts || {};
      this.el = container;
      this.height = opts.height || 260;
      this.unit = opts.unit || "won";
      this.bars = [];
      this.marks = [];
      this.guides = null;
      this.view = null; // {from, to} - bars 인덱스. null 이면 전체.
      this._yZoom = 1;   // 가격축 배율(1=자동 맞춤)
      this._yShift = 0;  // 가격축 이동(전체 범위 대비 비율)
      this._dragging = false;
      this._dragStartX = 0;
      this._dragStartView = null;
      this._lastLayout = null;

      this.el.classList.add("chartbox");
      this.el.style.position = "relative";

      this.canvas = document.createElement("canvas");
      this.canvas.style.display = "block";
      this.canvas.style.width = "100%";
      this.canvas.style.height = this.height + "px";
      this.el.appendChild(this.canvas);
      this.ctx = this.canvas.getContext("2d");

      this.tooltip = document.createElement("div");
      this.tooltip.style.position = "absolute";
      this.tooltip.style.pointerEvents = "none";
      this.tooltip.style.display = "none";
      this.tooltip.style.background = "rgba(20,20,20,0.9)";
      this.tooltip.style.color = "#fff";
      this.tooltip.style.padding = "6px 8px";
      this.tooltip.style.borderRadius = "4px";
      this.tooltip.style.fontSize = "12px";
      this.tooltip.style.lineHeight = "1.4";
      this.tooltip.style.zIndex = "10";
      this.tooltip.style.whiteSpace = "nowrap";
      this.el.appendChild(this.tooltip);

      this._ro = new ResizeObserver(() => this._resize());
      this._ro.observe(this.el);

      this._bindEvents();
      this._resize();
    }

    setData(data) {
      data = data || {};
      const prevLen = this.bars.length;
      const wasAtRight = this.view === null || (prevLen > 0 && this.view.to >= prevLen);
      const prevSpan = this.view ? this.view.to - this.view.from : null;

      this.bars = data.bars || [];
      this.marks = data.marks || [];
      this.guides = data.guides || null;
      if (data.unit) this.unit = data.unit;

      if (prevSpan !== null && this.bars.length !== prevLen) {
        if (wasAtRight) {
          // 오른쪽 끝을 보고 있었을 때만 오른쪽 끝을 계속 따라간다.
          const to = this.bars.length;
          const from = Math.max(0, to - prevSpan);
          this.view = { from, to };
        } else if (this.view.to > this.bars.length) {
          const span = this.view.to - this.view.from;
          const to = this.bars.length;
          const from = Math.max(0, to - span);
          this.view = { from, to };
        }
      }

      this.draw();
    }

    destroy() {
      this._ro.disconnect();
      this.el.removeEventListener("wheel", this._onWheel);
      this.el.removeEventListener("mousedown", this._onMouseDown);
      this.el.removeEventListener("mousemove", this._onMouseMove);
      this.el.removeEventListener("mouseleave", this._onMouseLeave);
      this.el.removeEventListener("dblclick", this._onDblClick);
      this.el.removeEventListener("touchstart", this._onTouchStart);
      this.el.removeEventListener("touchmove", this._onTouchMove);
      this.el.removeEventListener("touchend", this._onTouchEnd);
      window.removeEventListener("mouseup", this._onMouseUpWindow);
      this.el.innerHTML = "";
    }

    _view() {
      return this.view || { from: 0, to: this.bars.length };
    }

    // 체결 시각에 가장 가까운 봉. 분 단위(slice(0,16))로 맞춘다.
    _index(at) {
      if (!at || !this.bars.length) return -1;
      const key = String(at).slice(0, 16);
      for (let i = 0; i < this.bars.length; i++) {
        if (String(this.bars[i].timestamp || "").slice(0, 16) === key) return i;
      }
      const target = Date.parse(at);
      if (isNaN(target)) return -1;
      let best = -1;
      let bestDiff = Infinity;
      for (let i = 0; i < this.bars.length; i++) {
        const t = Date.parse(this.bars[i].timestamp);
        if (isNaN(t)) continue;
        const diff = Math.abs(t - target);
        if (diff < bestDiff) {
          bestDiff = diff;
          best = i;
        }
      }
      // ★ 봉 범위 밖의 체결(예: 몇 시간 전 매수)은 가장자리 봉에 잘못 붙이지 않는다.
      if (best >= 0 && this.bars.length > 1) {
        const t0 = Date.parse(this.bars[0].timestamp);
        const t1 = Date.parse(this.bars[this.bars.length - 1].timestamp);
        const step = (t1 - t0) / (this.bars.length - 1);
        if (step > 0 && bestDiff > step * 2) return -1;
      }
      return best;
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

    _colors() {
      const style = getComputedStyle(document.documentElement);
      return {
        rise: (style.getPropertyValue("--rise") || "#d64541").trim() || "#d64541",
        fall: (style.getPropertyValue("--fall") || "#3b6ee0").trim() || "#3b6ee0",
      };
    }

    draw() {
      const ctx = this.ctx;
      const w = this._cssWidth || this.el.clientWidth || 600;
      const h = this._cssHeight || this.height;
      ctx.clearRect(0, 0, w, h);
      if (!this.bars.length) return;

      // ★★ '전체 봉'이 아니라 '보이는 구간(view)'을 기준으로 좌표를 계산한다.
      // 일부만 바꾸면 봉 위치와 매수·매도 마커가 어긋난다.
      const view = this._view();
      const shown = this.bars.slice(view.from, view.to);
      if (!shown.length) return;

      const padLeft = 8;
      const padRight = 58;
      const padTop = 14;
      const padBottom = 18;
      const volH = Math.round(h * 0.18);
      const priceH = h - padTop - padBottom - volH;
      const graphW = w - padLeft - padRight;

      let min = Infinity;
      let max = -Infinity;
      for (const b of shown) {
        min = Math.min(min, b.lowPrice ?? b.closePrice);
        max = Math.max(max, b.highPrice ?? b.closePrice);
      }
      if (this.guides) {
        if (this.guides.stop != null) min = Math.min(min, this.guides.stop);
        if (this.guides.target != null) max = Math.max(max, this.guides.target);
      }
      if (min === max) {
        min -= 1;
        max += 1;
      }
      const pricePad = (max - min) * 0.08;
      min -= pricePad;
      max += pricePad;
      // ★ 가격축 줌·이동: 자동 맞춤 범위의 중심을 기준으로 좁히거나 옮긴다.
      {
        const full = max - min;
        const mid = (min + max) / 2 + this._yShift * full;
        const half = full / 2 / this._yZoom;
        min = mid - half;
        max = mid + half;
      }

      let maxVol = 0;
      for (const b of shown) maxVol = Math.max(maxVol, b.volume || 0);

      const n = shown.length;
      const slot = graphW / n;
      const bw = Math.max(1, Math.min(slot * 0.7, 14));

      const xAt = (i) => padLeft + i * slot + slot / 2;
      const yAt = (price) => padTop + ((max - price) / (max - min)) * priceH;
      const yVolTop = (v) => padTop + priceH + volH - (v / (maxVol || 1)) * (volH - 2);
      const volBase = padTop + priceH + volH;

      const colors = this._colors();

      // ★ 시세 축: 가로 격자 + 우측 가격 눈금, 아래쪽 시각 눈금(최대 5개).
      ctx.font = "10px sans-serif";
      ctx.lineWidth = 1;
      for (let k = 0; k <= 4; k++) {
        const v = min + ((max - min) * k) / 4;
        const y = yAt(v);
        ctx.strokeStyle = "rgba(127,127,127,0.18)";
        ctx.beginPath();
        ctx.moveTo(padLeft, y);
        ctx.lineTo(w - padRight, y);
        ctx.stroke();
        ctx.fillStyle = "rgba(127,127,127,0.95)";
        ctx.fillText(fmtAxis(v, this.unit), w - padRight + 4, y + 3);
      }
      const ticks = Math.min(5, n);
      let prevLab = null;
      for (let k = 0; k < ticks; k++) {
        const i = Math.round((k * (n - 1)) / Math.max(ticks - 1, 1));
        const ts = String(shown[i].timestamp || "");
        const lab = ts.length >= 16 ? ts.slice(11, 16) : "";
        if (!lab || lab === prevLab) continue;
        prevLab = lab;
        const tw = ctx.measureText(lab).width;
        ctx.fillStyle = "rgba(127,127,127,0.95)";
        ctx.fillText(lab, Math.max(padLeft, Math.min(xAt(i) - tw / 2, w - padRight - tw)), h - 5);
      }

      // ★ 가격축을 확대하면 봉·선이 가격 영역 밖으로 나가므로 그 영역 안에서만 그린다.
      ctx.save();
      ctx.beginPath();
      ctx.rect(padLeft - 1, padTop, graphW + 2, priceH);
      ctx.clip();
      for (let i = 0; i < n; i++) {
        const b = shown[i];
        const x = xAt(i);
        const rise = (b.closePrice ?? 0) >= (b.openPrice ?? b.closePrice ?? 0);
        const color = rise ? colors.rise : colors.fall;

        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, yAt(b.highPrice ?? b.closePrice));
        ctx.lineTo(x, yAt(b.lowPrice ?? b.closePrice));
        ctx.stroke();

        ctx.fillStyle = color;
        const oy = yAt(b.openPrice ?? b.closePrice);
        const cy = yAt(b.closePrice);
        const top = Math.min(oy, cy);
        const bh = Math.max(1, Math.abs(cy - oy));
        ctx.fillRect(x - bw / 2, top, bw, bh);
      }

      ctx.restore();
      // 거래량은 가격축 줌과 무관하게 아래 칸에 그대로.
      for (let i = 0; i < n; i++) {
        const b = shown[i];
        const rise = (b.closePrice ?? 0) >= (b.openPrice ?? b.closePrice ?? 0);
        ctx.fillStyle = rise ? colors.rise : colors.fall;
        ctx.globalAlpha = 0.45;
        const vy = yVolTop(b.volume || 0);
        ctx.fillRect(xAt(i) - bw / 2, vy, bw, Math.max(0, volBase - vy));
        ctx.globalAlpha = 1;
      }

      ctx.save();
      ctx.beginPath();
      ctx.rect(padLeft - 1, padTop, graphW + 2, priceH);
      ctx.clip();

      if (this.guides) {
        ctx.font = "11px sans-serif";
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1;
        if (this.guides.stop != null) {
          const y = yAt(this.guides.stop);
          ctx.strokeStyle = colors.fall;
          ctx.beginPath();
          ctx.moveTo(padLeft, y);
          ctx.lineTo(w - padRight, y);
          ctx.stroke();
          ctx.fillStyle = colors.fall;
          ctx.fillText("손절 " + fmtPrice(this.guides.stop, this.unit), w - padRight + 4, y + 4);
        }
        if (this.guides.target != null) {
          const y = yAt(this.guides.target);
          ctx.strokeStyle = colors.rise;
          ctx.beginPath();
          ctx.moveTo(padLeft, y);
          ctx.lineTo(w - padRight, y);
          ctx.stroke();
          ctx.fillStyle = colors.rise;
          ctx.fillText("목표 " + fmtPrice(this.guides.target, this.unit), w - padRight + 4, y + 4);
        }
        ctx.setLineDash([]);
      }

      // 매수·매도 표기: 봉 뒤 색 띠 + 굵은 세로선 + 체결가 큰 원 + 색 배경 라벨("매수 113,332") + 화살표.
      // 봉·거래량과 같은 색이라 묻히던 것을 흰 테두리와 색 배경 글자로 분명히 구분한다.
      const isUsd = this.unit === "usd";
      const shortPrice = (v) => (isUsd ? "$" + v.toFixed(2) : Math.abs(v) < 100 ? v.toFixed(2) : Math.round(v).toLocaleString("ko-KR"));
      const buyColor = "#e0342b";
      const sellColor = "#1f5fd6";
      const placed = [];  // 겹치는 라벨을 위아래로 밀어내기 위한 자리 기록
      for (const m of this.marks) {
        const gi = this._index(m.at);
        if (gi < 0) continue;
        const i = gi - view.from;
        if (i < 0 || i >= n) continue;

        const x = xAt(i);
        const y = yAt(m.price);
        const isBuy = m.kind === "buy";
        const color = isBuy ? buyColor : sellColor;

        // 배경 띠(그 봉이 어디인지)
        ctx.fillStyle = isBuy ? "rgba(224,52,43,0.14)" : "rgba(31,95,214,0.14)";
        ctx.fillRect(x - Math.max(slot, 6) / 2 - 1, padTop, Math.max(slot, 6) + 2, priceH);

        // 세로선
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(x, padTop);
        ctx.lineTo(x, padTop + priceH);
        ctx.stroke();
        ctx.setLineDash([]);

        // 체결가 원(흰 테두리 + 색 채움)
        ctx.beginPath();
        ctx.arc(x, y, 6, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 2.5;
        ctx.strokeStyle = "#fff";
        ctx.stroke();

        // 라벨: 매수는 체결가 아래, 매도는 위. 이미 놓인 라벨과 겹치면 반대 방향/더 멀리로.
        ctx.font = "bold 12px sans-serif";
        const text = (isBuy ? "▲ 매수 " : "▼ 매도 ") + shortPrice(m.price);
        const tw = ctx.measureText(text).width + 12;
        const th = 20;
        let lx = Math.min(Math.max(x - tw / 2, padLeft), w - padRight - tw);
        let ly = isBuy ? y + 12 : y - 12 - th;
        const dir = isBuy ? 1 : -1;
        for (let tries = 0; tries < 6; tries++) {
          const hit = placed.some((r) => lx < r.x + r.w && lx + tw > r.x && ly < r.y + r.h && ly + th > r.y);
          if (!hit) break;
          ly += dir * (th + 2);
        }
        ly = Math.min(Math.max(ly, padTop), padTop + priceH - th);
        placed.push({ x: lx, y: ly, w: tw, h: th });
        // 라벨과 체결가 원을 잇는 선
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, y + (isBuy ? 6 : -6));
        ctx.lineTo(x, isBuy ? ly : ly + th);
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(lx, ly, tw, th, 4); else ctx.rect(lx, ly, tw, th);
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.fillText(text, lx + 6, ly + 14);
      }

      ctx.restore();

      if (this.view) {
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.font = "11px sans-serif";
        ctx.fillText(n + "/" + this.bars.length + "봉 · 두 번 누르면 전체", padLeft, padTop - 3);
      } else if (this._yZoom !== 1) {
        ctx.fillStyle = "rgba(127,127,127,0.95)";
        ctx.font = "11px sans-serif";
        ctx.fillText("가격 ×" + this._yZoom.toFixed(1) + " · 두 번 누르면 전체", padLeft, padTop - 3);
      }

      this._lastLayout = { padLeft, padRight, padTop, padBottom, volH, priceH, graphW, slot, bw, min, max, maxVol, view, shown };
    }

    // 시간축 줌(zoomIn=true 면 확대). 커서 위치의 봉을 기준으로 늘리고 줄인다.
    _zoomTime(zoomIn, cursorX) {
      if (!this.bars.length) return;
      const total = this.bars.length;
      const view = this._view();
      const span = view.to - view.from;
      const layout = this._lastLayout;
      const padLeft = layout ? layout.padLeft : 8;
      const graphW = layout ? layout.graphW : this._cssWidth - padLeft - 58;
      const frac = Math.min(1, Math.max(0, ((cursorX == null ? padLeft + graphW / 2 : cursorX) - padLeft) / graphW));
      const anchor = view.from + span * frac;
      let next = Math.round(span * (zoomIn ? 0.8 : 1.25));
      if (next === span) next += zoomIn ? -1 : 1;
      next = Math.min(Math.max(next, 12), total);
      if (next >= total) { this.view = null; return; }
      let from = Math.round(anchor - next * frac);
      from = Math.min(Math.max(from, 0), total - next);
      this.view = { from, to: from + next };
    }

    // 가격축 줌.
    _zoomPrice(zoomIn) {
      const f = zoomIn ? 1.25 : 0.8;
      this._yZoom = Math.max(1, Math.min(this._yZoom * f, 30));
      if (this._yZoom === 1) this._yShift = 0;
    }

    _resetZoom() {
      this.view = null;
      this._yZoom = 1;
      this._yShift = 0;
      this.draw();
    }

    _bindEvents() {
      this._onWheel = (e) => {
        // ★★ preventDefault() 를 조기 return 보다 먼저 부른다.
        // 뒤에 두면 봉이 아직 없을 때 페이지가 스크롤돼 "어떤 때는 되고
        // 어떤 때는 안 된다"가 된다.
        e.preventDefault();
        e.stopPropagation();
        if (!this.bars.length) return;

        // ★ 위치에 따라 축이 정해진다: 오른쪽 가격 눈금 위 = 가격축만, 아래 시각 눈금 위 = 시간축만, 본문 = 둘 다.
        const rect = this.el.getBoundingClientRect();
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        const layout = this._lastLayout;
        const padRight = layout ? layout.padRight : 58;
        const padBottom = layout ? layout.padBottom : 18;
        const onY = cx >= this._cssWidth - padRight;
        const onX = !onY && cy >= this._cssHeight - padBottom;
        if (!onY) this._zoomTime(e.deltaY < 0, cx);
        if (!onX) this._zoomPrice(e.deltaY < 0);
        this.draw();
      };
      // ★ 리스너는 canvas 가 아니라 컨테이너(this.el)에 건다 - 캔버스 여백이나
      // 툴팁 위에서 굴리면 canvas 리스너는 안 걸린다.
      this.el.addEventListener("wheel", this._onWheel, { passive: false });

      this._onMouseDown = (e) => {
        if (!this.view && this._yZoom === 1) return; // 확대된 상태에서만 끌어서 이동한다.
        this._dragging = true;
        this._dragStartX = e.clientX;
        this._dragStartY = e.clientY;
        this._dragStartShift = this._yShift;
        this._dragStartView = this.view ? { from: this.view.from, to: this.view.to } : null;
      };
      this.el.addEventListener("mousedown", this._onMouseDown);

      this._onMouseMove = (e) => {
        const rect = this.el.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        if (this._dragging) {
          const layout = this._lastLayout;
          const slot = layout ? layout.slot : 10;
          if (this._dragStartView) {
            const dx = e.clientX - this._dragStartX;
            const shiftBars = Math.round(-dx / slot);
            const span = this._dragStartView.to - this._dragStartView.from;
            let from = this._dragStartView.from + shiftBars;
            from = Math.min(Math.max(from, 0), this.bars.length - span);
            this.view = { from, to: from + span };
          }
          if (this._yZoom !== 1 && layout) {
            const dy = e.clientY - this._dragStartY;
            this._yShift = this._dragStartShift + dy / layout.priceH / this._yZoom;
          }
          this.draw();
          return;
        }

        this._showTooltip(x, y);
      };
      this.el.addEventListener("mousemove", this._onMouseMove);

      this._onMouseLeave = () => {
        this.tooltip.style.display = "none";
      };
      this.el.addEventListener("mouseleave", this._onMouseLeave);

      // mouseup 은 window 에 건다 - 드래그 중 컨테이너 밖으로 나가도 놓친 걸 알아채야 한다.
      this._onMouseUpWindow = () => {
        this._dragging = false;
      };
      window.addEventListener("mouseup", this._onMouseUpWindow);

      this._onDblClick = () => this._resetZoom();
      this.el.addEventListener("dblclick", this._onDblClick);

      // ★ 확대/축소/처음으로 버튼 - 휠이 없는 화면(모바일)이나 축별로 정확히 조절하고 싶을 때.
      //   시간(가로)과 가격(세로)을 따로 누를 수 있다.
      const bar = document.createElement("div");
      bar.className = "chart-zoombar";
      const mk = (label, title, fn) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = label;
        b.title = title;
        b.addEventListener("click", (ev) => { ev.stopPropagation(); fn(); this.draw(); });
        b.addEventListener("dblclick", (ev) => ev.stopPropagation());
        bar.appendChild(b);
      };
      mk("↔＋", "시간축 확대", () => this._zoomTime(true, null));
      mk("↔－", "시간축 축소", () => this._zoomTime(false, null));
      mk("↕＋", "가격축 확대", () => this._zoomPrice(true));
      mk("↕－", "가격축 축소", () => this._zoomPrice(false));
      mk("⟲", "처음 상태로", () => this._resetZoom());
      this.el.appendChild(bar);
      this._zoomBar = bar;

      // ★ 터치: 두 손가락 핀치(가로로 벌리면 시간축, 세로로 벌리면 가격축 확대), 한 손가락 끌기로 이동, 더블탭 초기화.
      this.canvas.style.touchAction = "pan-y";
      let t0 = null;
      let lastTap = 0;
      const pts = (t) => ({ dx: Math.abs(t[0].clientX - t[1].clientX), dy: Math.abs(t[0].clientY - t[1].clientY) });
      this._onTouchStart = (e) => {
        if (e.touches.length === 2) {
          t0 = Object.assign(pts(e.touches), { mode: "pinch" });
        } else if (e.touches.length === 1) {
          const now = Date.now();
          if (now - lastTap < 300) this._resetZoom();
          lastTap = now;
          t0 = { mode: "pan", x: e.touches[0].clientX, view: this.view ? { from: this.view.from, to: this.view.to } : null };
        }
      };
      this._onTouchMove = (e) => {
        if (!t0) return;
        if (t0.mode === "pinch" && e.touches.length === 2) {
          e.preventDefault();
          const p = pts(e.touches);
          const rx = t0.dx > 8 ? p.dx / t0.dx : 1;
          const ry = t0.dy > 8 ? p.dy / t0.dy : 1;
          let done = false;
          if (rx > 1.12 || rx < 0.89) { this._zoomTime(rx > 1, null); t0.dx = p.dx; done = true; }
          if (ry > 1.12 || ry < 0.89) { this._zoomPrice(ry > 1); t0.dy = p.dy; done = true; }
          if (done) this.draw();
        } else if (t0.mode === "pan" && e.touches.length === 1 && t0.view && this.view) {
          const layout = this._lastLayout;
          if (!layout) return;
          const dx = e.touches[0].clientX - t0.x;
          if (Math.abs(dx) > 6) {
            e.preventDefault();
            const span = t0.view.to - t0.view.from;
            let from = t0.view.from + Math.round(-dx / layout.slot);
            from = Math.min(Math.max(from, 0), this.bars.length - span);
            this.view = { from, to: from + span };
            this.draw();
          }
        }
      };
      this._onTouchEnd = () => { t0 = null; };
      this.el.addEventListener("touchstart", this._onTouchStart, { passive: true });
      this.el.addEventListener("touchmove", this._onTouchMove, { passive: false });
      this.el.addEventListener("touchend", this._onTouchEnd, { passive: true });
    }

    _showTooltip(x, y) {
      const layout = this._lastLayout;
      if (!layout) {
        this.tooltip.style.display = "none";
        return;
      }
      const { padLeft, slot, shown, view } = layout;
      const i = Math.floor((x - padLeft) / slot);
      if (i < 0 || i >= shown.length) {
        this.tooltip.style.display = "none";
        return;
      }
      const b = shown[i];

      const marksHere = this.marks.filter((m) => {
        const gi = this._index(m.at);
        return gi - view.from === i;
      });

      let html =
        "<div>" + String(b.timestamp || "").slice(0, 16).replace("T", " ") + "</div>" +
        "<div>시 " + fmtPrice(b.openPrice ?? b.closePrice, this.unit) + " · 고 " + fmtPrice(b.highPrice ?? b.closePrice, this.unit) + "</div>" +
        "<div>저 " + fmtPrice(b.lowPrice ?? b.closePrice, this.unit) + " · 종 " + fmtPrice(b.closePrice, this.unit) + "</div>" +
        "<div>거래량 " + Math.round(b.volume || 0).toLocaleString("ko-KR") + "</div>";

      for (const m of marksHere) {
        html +=
          '<div style="margin-top:4px;border-top:1px solid rgba(255,255,255,0.3);padding-top:4px;">' +
          (m.kind === "buy" ? "매수" : "매도") + " " + fmtPrice(m.price, this.unit) + " × " + m.qty +
          (m.pnl != null ? " (" + (m.pnl >= 0 ? "+" : "") + fmtPrice(m.pnl, this.unit) + ")" : "") +
          (m.reason ? "<br>" + escapeHtml(m.reason) : "") +
          "</div>";
      }

      this.tooltip.innerHTML = html;
      this.tooltip.style.display = "block";
      const left = Math.min(x + 12, Math.max(0, this._cssWidth - 170));
      this.tooltip.style.left = left + "px";
      this.tooltip.style.top = Math.max(0, y - 10) + "px";
    }
  }

  function fmtPrice(v, unit) {
    if (v == null || isNaN(v)) return "-";
    if (unit === "usd") return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    // 저가 코인은 소수점이 있어야 봉이 구분된다.
    const a = Math.abs(v);
    if (a < 100) return v.toLocaleString("ko-KR", { maximumFractionDigits: 2 }) + "원";
    return Math.round(v).toLocaleString("ko-KR") + "원";
  }
  // 우측 눈금용 짧은 표기(폭이 좁다).
  function fmtAxis(v, unit) {
    if (unit === "usd") return "$" + v.toFixed(2);
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e4).toLocaleString("ko-KR", { maximumFractionDigits: 0 }) + "만";
    if (a < 100) return v.toFixed(2);
    return Math.round(v).toLocaleString("ko-KR");
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = String(s);
    return div.innerHTML;
  }

  async function tradeChart(container, symbol, opts) {
    opts = opts || {};
    const height = opts.height || 260;
    container.innerHTML =
      '<div class="chart-skeleton" style="height:' + height + 'px;background:rgba(127,127,127,0.12);border-radius:6px;"></div>';

    let data;
    try {
      const qs = new URLSearchParams();
      if (opts.date) qs.set("date", opts.date);
      if (opts.count) qs.set("count", String(opts.count));
      const resp = await fetch("/api/chart/" + encodeURIComponent(symbol) + "?" + qs.toString());
      data = await resp.json();
    } catch (e) {
      container.innerHTML = '<div class="chart-error">분봉을 불러오지 못했습니다.</div>';
      return null;
    }

    if (!data || !data.bars || !data.bars.length) {
      container.innerHTML =
        '<div class="chart-error">이 종목의 분봉을 받지 못했습니다. 인터넷 시세는 최근 며칠치만 제공합니다.</div>';
      return null;
    }

    container.innerHTML = "";
    const chart = new CandleChart(container, { height });

    let guides = null;
    const buyMark = (data.marks || []).find((m) => m.kind === "buy");
    const entry = opts.entry != null ? opts.entry : buyMark ? buyMark.price : null;
    if (entry != null) {
      guides = {
        stop: entry * (1 - (data.stop_pct || 0)),
        target: entry * (1 + (data.target_pct || 0)),
      };
    }

    chart.setData({ bars: data.bars, marks: data.marks || [], guides });

    if (data.marks && data.marks.length) {
      const note = document.createElement("div");
      note.style.fontSize = "12px";
      note.style.color = "var(--muted, #888)";
      note.style.marginTop = "4px";
      note.textContent = "▲ 매수 · ▼ 매도 — 봉 위에 마우스를 올리면 자세히 보입니다.";
      container.appendChild(note);
    }

    return chart;
  }

  window.UI = Object.assign(window.UI || {}, { CandleChart, tradeChart });
})();
