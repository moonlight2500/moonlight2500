"use strict";
(function () {
  function escAttr(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function _fieldHelp(text) {
    // ★★★ 설정 필드의 힌트를 라벨 옆 아이콘으로 옮긴다 - 본문에 한 줄씩
    // 깔면(필드 104개) 스크롤이 감당이 안 된다.
    // ★ app.js 의 infoIcon 과 같은 모양·동작이지만, forms.js 는 별도
    //   스코프라 여기에 따로 둔다(같은 CSS 클래스를 써서 생김새는 같다).
    const wrap = document.createElement("span");
    wrap.className = "info-wrap";
    const btn = document.createElement("button");
    btn.className = "info-dot";
    btn.type = "button";           // ★ 폼 안이라 type 을 안 주면 submit 이 된다.
    btn.textContent = "?";
    btn.title = text;
    btn.setAttribute("aria-label", "설명 보기");
    const pop = document.createElement("div");
    pop.className = "info-pop";
    pop.textContent = text;
    pop.style.display = "none";

    let pinned = false;
    wrap.onmouseenter = () => { if (!pinned) pop.style.display = "block"; };
    wrap.onmouseleave = () => { if (!pinned) pop.style.display = "none"; };
    btn.onclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      document.querySelectorAll(".info-pop").forEach((x) => { if (x !== pop) x.style.display = "none"; });
      pinned = !pinned;
      pop.style.display = pinned ? "block" : "none";
    };
    wrap.appendChild(btn);
    wrap.appendChild(pop);
    return wrap;
  }

  function getPath(obj, path) {
    return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  }

  function setPath(obj, path, value) {
    const keys = path.split(".");
    let cur = obj;
    for (let i = 0; i < keys.length - 1; i++) {
      const k = keys[i];
      if (cur[k] == null || typeof cur[k] !== "object") cur[k] = {};
      cur = cur[k];
    }
    cur[keys[keys.length - 1]] = value;
  }

  // ━━ 설정 스키마 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // ★★ JSON 편집기 대신 항목마다 이름·입력 형식·"왜 중요한지"를 붙인 폼으로 만든다.


  // ━━ 입력 분류: 필수 / 선택 / 자동 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // 필수: 사용자가 반드시 정해야 하는 값(시장 사용 여부·거래 모드·투자금액·동시 보유·매매 속도).
  // 선택: 넣으면 더 좋지만 비워도 프로그램이 알아서 하는 값(직접 추가 관심 종목).
  // 자동: 프로그램이 정한 기본값 - 그대로 두는 것을 권장(손절·익절·주기·필터·기법 파라미터 등).
  const REQUIRED_PATHS = new Set([
    "crypto.enabled", "overseas.enabled", "swing.enabled", "mode", "crypto.mode", "overseas.mode", "swing.mode",
    "risk.style", "overseas.style", "crypto.style",
    "capital.allocation", "capital.max_positions", "overseas.budget_usd", "overseas.max_positions",
    "crypto.budget", "crypto.max_positions", "swing.budget", "swing.max_positions",
  ]);
  const OPTIONAL_PATHS = new Set([
    "screen.watchlist", "overseas.watchlist", "crypto.watchlist",
    "swing.watchlist", "swing.overseas_watchlist", "swing.crypto_watchlist",
  ]);

  function fieldTier(path) {
    if (REQUIRED_PATHS.has(path)) return "required";
    if (OPTIONAL_PATHS.has(path)) return "optional";
    return "auto";
  }

  const CONFIG_SCHEMA = [
    {
      title: "거래할 시장", group: "거래선택",
      note: "국내주식·암호화폐·해외주식은 각자 다른 동작모드·자금·비용·위험관리 설정을 따로 씁니다 - 서로 섞이지 않습니다. "
        + "'공통' 그룹(투자금액 배분·매매 속도)만 시장과 무관하게 공유합니다. "
        + "국내주식은 이 프로그램의 기본 대상이라 껐다 켰다 할 수는 없고, 대신 아래 모드로 실제 매매 여부를 정합니다. 암호화폐·해외주식은 켜면 [준비·연결]에서 계정을 등록해야 합니다.",
      fields: [
        { path: "mode", label: "국내주식 모드", kind: "select",
          options: [
            ["web", "관찰(web) - 신호만 보고 주문은 내지 않습니다"],
            ["sim", "시뮬레이션(sim) - 가짜 시장으로 연습합니다"],
            ["replay", "리플레이(replay) - 과거 데이터를 재생합니다"],
            ["paper", "모의매매(paper) - 실시간 시세로 가짜 주문을 냅니다"],
            ["live", "실거래(live) - 실제 계좌로 진짜 주문을 냅니다"],
          ],
          hint: "★ 국내주식은 끌 수 없는 대신, '관찰(web)'로 두면 사실상 매매를 안 하는 것과 같습니다(신호만 보고 주문은 안 냄). 실제 조정은 [국내주식] → '동작 모드'에서도 똑같이 할 수 있습니다(같은 값)." },
        { path: "crypto.enabled", label: "암호화폐(빗썸) 사용", kind: "bool",
          hint: "켜면 변동성 돌파 전략으로 자동매매를 시작할 수 있습니다. 세부 설정은 아래 '암호화폐' 섹션에서." },
        { path: "overseas.enabled", label: "해외주식 사용", kind: "bool",
          hint: "켜면 국내주식과 같은 기법·계좌로 자동매매를 시작할 수 있습니다. 세부 설정은 아래 '해외주식' 섹션에서." },
        { path: "swing.enabled", label: "스윙(며칠~몇 주 보유) 사용", kind: "bool",
          hint: "켜면 국내주식과 같은 계좌로, 일봉 기준 스윙 전용 기법으로 자동매매를 시작할 수 있습니다. 세부 설정은 아래 '스윙' 섹션에서." },
      ],
    },
    {
      title: "동작 모드", group: "국내주식",
      note: "이 모드는 국내주식에만 적용됩니다 - 암호화폐는 아래 '암호화폐(빗썸)' 섹션의 '암호화폐 실거래' 스위치로 완전히 따로 켭니다.",
      fields: [
        {
          path: "mode", label: "모드", kind: "select",
          options: [
            ["web", "관찰(web) - 신호만 보고 주문은 내지 않습니다"],
            ["sim", "시뮬레이션(sim) - 가짜 시장으로 연습합니다"],
            ["replay", "리플레이(replay) - 과거 데이터를 재생합니다"],
            ["paper", "모의매매(paper) - 실시간 시세로 가짜 주문을 냅니다"],
            ["live", "실거래(live) - 실제 계좌로 진짜 주문을 냅니다"],
          ],
          hint: "live 로 바꾸는 순간부터 여기서 나가는 주문은 진짜 돈을 움직입니다.",
        },
        {
          path: "account_seq", label: "계좌 번호(내부용)", kind: "num",
          hint: "여러 계좌가 있을 때 어느 계좌를 쓸지 지정합니다. 비워 두면 첫 번째 위탁계좌를 씁니다.",
        },
      ],
    },
    {
      title: "투자금액 · 동시 보유 (필수 입력)", group: "거래선택",
      note: "★ 사용자가 반드시 정해야 하는 값입니다. 시장별 '투자금액'(이 프로그램에 맡길 총액)과 '동시 보유 종목 수'만 정하면 "
        + "종목당 한도(= 투자금액 ÷ 동시 보유 수)와 1회 매수 금액(신호 강도에 따라 변동)·추가 매수·분할 매도는 자동으로 정해집니다. "
        + "(공통 그룹의 '투자금액 배분·분할 매매'에서 자동 규칙을 볼 수 있습니다.)",
      fields: [
        { path: "capital.allocation", label: "국내주식 투자금액(원)", kind: "won", step: 100000,
          hint: "계좌 전체가 아니라 국내주식 자동매매에 맡길 금액만 적습니다. 나머지 자산은 건드리지 않습니다. 초기값 1,000만원." },
        { path: "capital.max_positions", label: "국내주식 동시 보유 종목 수", kind: "num", min: 1, max: 10, step: 1,
          hint: "종목당 한도 = 투자금액 ÷ 이 값. 종목이 많아질수록 감시가 허술해지니 적게 시작하세요." },
        { path: "overseas.budget_usd", label: "해외주식 투자금액($)", kind: "num", min: 0, step: 100,
          hint: "해외주식 자동매매에 맡길 총액(달러). 초기값 10,000달러." },
        { path: "overseas.max_positions", label: "해외주식 동시 보유 종목 수", kind: "num", min: 1, max: 20, step: 1,
          hint: "종목당 한도 = 투자금액 ÷ 이 값." },
        { path: "crypto.budget", label: "암호화폐 투자금액(원)", kind: "won", step: 100000,
          hint: "암호화폐 자동매매에 맡길 총액(원). 초기값 1,000만원." },
        { path: "crypto.max_positions", label: "암호화폐 동시 보유 코인 수", kind: "num", min: 1, max: 30, step: 1,
          hint: "종목당 한도 = 투자금액 ÷ 이 값. 감시 종목이 늘어도 동시 보유는 이 수로 제한됩니다." },
        { path: "swing.budget", label: "스윙 투자금액", kind: "won", step: 100000,
          hint: "스윙 자동매매에 맡길 총액(국내는 원, 해외·암호화폐는 환산 없이 같은 예산에 섞어 씀 - 근사치). 초기값 1,000만원." },
        { path: "swing.max_positions", label: "스윙 동시 보유 종목 수", kind: "num", min: 1, max: 20, step: 1,
          hint: "종목당 한도 = 투자금액 ÷ 이 값." },
      ],
    },
    {
      title: "매매 속도 (필수 선택, 시장별)", group: "거래선택",
      note: "★ \"단타 매매 모드를 시장별로 분리해\" 요청에 따라 국내·해외·암호화폐가 각자 다른 속도를 쓸 수 있습니다(예: 국내만 빠른 단타, 암호화폐는 보통). "
        + "보통은 설정한 값 그대로, 빠른 단타는 시세 확인 주기와 진입 기법 수만 바꿉니다(손절·익절 폭은 그대로). 스캘핑(실험용)만 거기에 손절·익절 폭까지 자동으로 좁힙니다. "
        + "스윙은 며칠~몇 주 보유가 전제라 이 개념이 없습니다(자기 설정값을 그대로 씁니다). "
        + "구조적 한계 - 해외주식은 국내주식과 진입 기법 목록을 공유해서, '빠른 단타 전용 진입 기법 추가'는 국내 속도로만 판단합니다(해외 속도는 손절·익절·시세 확인 주기에만 적용). "
        + "손절폭은 어떤 속도든 검증된 값(2.5%) 그대로입니다 - 한때 1.8%로 좁혔더니 실거래(모의매매) 승률이 눈에 띄게 나빠지는 게 확인돼(해외 3건이 거의 동시에 손절되는 등) 되돌렸습니다. "
        + "⚠ 시뮬레이션(옛 fast 폭 기준)에서 보통 설정은 승률 70%·손익비 2.8, fast(그때는 폭이 보통과 같았음)는 손익비 약 1.8~2.4, scalp는 승률 13~20%·손익비 0.13 이하로 크게 손실이 났습니다 - 모의매매로 순이익을 확인한 뒤 쓰세요.",
      fields: [
        { path: "risk.style", label: "국내주식 매매 속도", kind: "select", options: [
            ["normal", "보통(normal) - 아래 설정값 그대로"],
            ["fast", "빠른 단타(fast) - 시세 확인을 더 자주, 진입 기법을 더 다양하게. 손절 2.5%/익절 5%"],
            ["scalp", "스캘핑(scalp, 실험용) - fast 보다 더 작은 목표(익절 2.4%/손절 1.2%)로 자주 거래"],
          ],
          hint: "fast 는 시세 확인 15초·재선정 10분·재진입 금지 5분·일일 거래 30회, 진입 기법 6개 추가를 자동으로 정합니다." },
        { path: "overseas.style", label: "해외주식 매매 속도", kind: "select", options: [
            ["normal", "보통(normal) - 아래 설정값 그대로"],
            ["fast", "빠른 단타(fast) - 보유 종목 확인을 더 자주. 손절 2.5%/익절 5%"],
            ["scalp", "스캘핑(scalp, 실험용) - fast 보다 더 작은 목표(익절 2.4%/손절 1.2%)로 자주 거래"],
          ],
          hint: "fast 는 보유 종목 확인 15초를 자동으로 정합니다. 진입 기법 목록은 국내 속도를 따릅니다(위 안내 참고)." },
        { path: "crypto.style", label: "암호화폐 매매 속도", kind: "select", options: [
            ["normal", "보통(normal) - 아래 설정값 그대로"],
            ["fast", "빠른 단타(fast) - 판단 주기를 더 자주, 진입 기법을 더 다양하게. 손절 2.5%/익절 5%"],
            ["scalp", "스캘핑(scalp, 실험용) - fast 보다 더 작은 목표(익절 3%/손절 1.5%)로 자주 거래"],
          ],
          hint: "fast 는 판단 주기 10초·재진입 금지 10분·진입 기법 6개 추가를 자동으로 정합니다." },
      ],
    },
    {
      title: "국내주식 거래 세션", group: "국내주식",
      note: "★ \"국장도 8시부터 9시까지 프리장, 9시부터 3시반까지 본장, 이후 NXT장 - 장 별로 거래를 할지 사용자가 선택하게 해\" 요청. "
        + "국내주식도 해외주식처럼 세션별로 신규 매수를 켜고 끌 수 있습니다(청산은 세션과 무관하게 항상 계속됩니다). "
        + "프리장·NXT장은 기본이 꺼짐입니다 - 지금까지 국내주식은 정규장에서만 매수해 왔으니, 아무것도 안 바꾸면 예전과 똑같이 동작합니다. "
        + "★ 세션을 켜도, 실제로 그 시간대까지 신호를 평가하려면 [설정] → 매매 시간대에서 진입 시작·종료 시각도 그 세션을 포함하도록 넓혀야 합니다(예: 프리장을 켜려면 진입 시작 시각을 08:00대로 당겨야 합니다) - 이 토글은 \"그 시간대에 신호가 통과하면 실제로 사도 되는지\"만 결정합니다.",
      fields: [
        { path: "risk.trade_premarket", label: "프리장 거래(08:00~09:00)", kind: "bool",
          hint: "동시호가 시간대입니다." },
        { path: "risk.trade_regular", label: "본장(정규장) 거래(09:00~15:30)", kind: "bool",
          hint: "가장 유동성이 좋은 정규장입니다." },
        { path: "risk.trade_nxt", label: "NXT장 거래(15:30~20:00)", kind: "bool",
          hint: "정규장 마감 이후 시간외 거래입니다." },
      ],
    },
    {
      title: "투자금액 배분 · 분할 매수/매도 (자동)", group: "공통",
      note: "★ 프로그램이 자동으로 정하는 규칙입니다(국내·해외·암호화폐·스윙 공통). 기본값 그대로 쓰는 것을 권장하며, 바꾸고 싶을 때만 수정하세요. "
        + "첫 매수 = 종목당 한도 × 첫 매수 비율 × 신호 배수, 이후 이익이 날 때만 나눠서 추가 매수, 이익 구간에서 여러 번 나눠 매도합니다.",
      fields: [
        { path: "sizing.signal_sizing", label: "신호 강도에 따라 매수 금액 조절", kind: "bool",
          hint: "켜면 진입 기준을 크게 넘긴 강한 신호는 더 사고(최대 배수), 간신히 통과한 약한 신호는 덜 삽니다(최소 배수)." },
        { path: "sizing.dynamic", label: "테마 근거·장중 변동성으로 금액 자동 조절", kind: "bool",
          hint: "켜면 ① 첫 매수 배수를 '신호 강도 60% + 테마 근거 크기 40%'(테마 순위·동반 상승 종목 수·상승률 중앙값·대장주 여부)로 정하고, ② 장중 변동성(분봉 ATR)이 큰 종목은 적게(0.6~1.2배), "
            + "③ 추가 매수는 근거가 큰 종목을 더, ④ 분할 매도는 근거가 크면 덜·출렁임이 크면 더 팝니다. 끄면 신호 강도만 반영합니다." },
        { path: "sizing.min_mult", label: "약한 신호 매수 배수", kind: "num", min: 0.1, max: 1, step: 0.05,
          hint: "가장 약한 신호의 첫 매수 배수(기본 0.6)." },
        { path: "sizing.max_mult", label: "강한 신호 매수 배수", kind: "num", min: 1, max: 3, step: 0.05,
          hint: "가장 강한 신호의 첫 매수 배수(기본 1.4). 종목당 한도는 어떤 경우에도 넘지 않습니다." },
        { path: "sizing.scale_in", label: "추가 매수(피라미딩)", kind: "bool",
          hint: "이익 중인 종목에만, 진입 신호가 유지될 때, 종목당 한도 안에서 나눠서 더 삽니다. 손실 중에는 절대 추가하지 않습니다(물타기 금지)." },
        { path: "sizing.initial_ratio", label: "첫 매수 비율", kind: "pct", min: 20, max: 100, step: 5, suffix: "%",
          hint: "종목당 한도 중 처음에 사는 비율(기본 50%). 나머지는 추가 매수 몫입니다." },
        { path: "sizing.max_adds", label: "추가 매수 최대 횟수", kind: "num", min: 0, max: 5, step: 1,
          hint: "기본 2회. 남은 몫을 이 횟수로 균등 분할합니다." },
        { path: "sizing.add_step_pct", label: "추가 매수 간격(0=자동)", kind: "pct", min: 0, max: 20, step: 0.1, suffix: "%",
          hint: "마지막 매수가보다 이만큼 오르면 추가합니다. 0 이면 자동(손절폭의 40%)." },
        { path: "sizing.scale_out", label: "분할 매도", kind: "bool",
          hint: "켜면 익절폭의 절반에서 1차, 익절폭에서 2차로 나눠 팔고, 나머지는 추적 손절로 끌고 갑니다(이익을 더 키울 여지). 끄면 익절에서 전량 청산합니다." },
        { path: "sizing.first_exit_at", label: "1차 분할 매도 수익률(0=자동)", kind: "pct", min: 0, max: 30, step: 0.1, suffix: "%",
          hint: "0 이면 자동(익절폭의 절반)." },
        { path: "sizing.first_exit_ratio", label: "1차 매도 비율", kind: "pct", min: 5, max: 90, step: 1, suffix: "%",
          hint: "1차에 파는 비율(보유량 기준, 기본 34%)." },
        { path: "sizing.second_exit_ratio", label: "2차 매도 비율", kind: "pct", min: 5, max: 95, step: 1, suffix: "%",
          hint: "익절폭 도달 시 남은 수량 중 파는 비율(기본 50%). 그 나머지(약 1/3)는 추적 손절이 끌고 갑니다." },
        { path: "sizing.breakeven_after_first", label: "1차 매도 뒤 본전 방어", kind: "bool",
          hint: "1차 분할 매도로 이익을 챙긴 뒤 가격이 본전 아래로 내려오면 나머지를 정리합니다." },
        { path: "sizing.take_profit_adx_threshold", label: "익절폭 확대 시작 기준(ADX)", kind: "num", min: 0, max: 100, step: 1,
          hint: "추세 세기(ADX, 방향 무관 0~100)가 이 값 이상이면 익절 목표를 넓히기 시작합니다(기본 25 - 통상 20~25 위면 추세로 봅니다)." },
        { path: "sizing.take_profit_adx_full", label: "익절폭 최대 확대 기준(ADX)", kind: "num", min: 0, max: 100, step: 1,
          hint: "ADX 가 이 값에 이르면 '최대 몇 배까지 확대'(아래 값)에 도달합니다. 그 사이는 비례해서 넓어집니다." },
        { path: "sizing.take_profit_max_widen", label: "익절폭 최대 확대 배수", kind: "num", min: 1, max: 5, step: 0.1,
          hint: "추세가 가장 강할 때 기본 익절폭의 몇 배까지 넓힐지(기본 2배). 1로 두면 안 넓힙니다." },
      ],
    },
    {
      title: "위험 관리", group: "국내주식",
      note: "이 프로그램에서 가장 중요한 부분입니다. 암호화폐·해외주식의 손절·익절·추적청산·최대보유시간·일일 최대 거래 횟수는 각자 별도 값(아래 '암호화폐(빗썸)'·'해외주식' 섹션)을 씁니다 - 변동성이 달라 같은 기준을 못 씁니다. 다만 아래 '연속 손절 한도·재진입 금지 시간·재개 간격'은 해외주식에도 그대로 적용됩니다(해외주식은 별도 값이 없습니다).",
      fields: [
        {
          path: "risk.stop_loss_pct", label: "손절 폭", kind: "pct", min: 0.1, max: 20, step: 0.1, suffix: "%",
          hint: "진입가 대비 이만큼 밀리면 시장가로 즉시 정리합니다. 버티지 않습니다.",
        },
        {
          path: "risk.take_profit_pct", label: "익절 폭", kind: "pct", min: 0.1, max: 50, step: 0.1, suffix: "%",
          hint: "왕복 비용의 2배보다 좁게 잡으면 매매가 아예 거부됩니다 - 비용을 이기지 못하는 설정이기 때문입니다.",
        },
        {
          path: "risk.trailing_stop_pct", label: "추적 손절 하락폭", kind: "pct", min: 0.1, max: 20, step: 0.1, suffix: "%",
          hint: "고점을 찍은 뒤 이만큼 되밀리면 남은 수익을 지키기 위해 정리합니다.",
        },
        {
          path: "risk.trailing_arm_pct", label: "추적 손절 발동 기준", kind: "pct", min: 0.1, max: 20, step: 0.1, suffix: "%",
          hint: "수익이 이만큼 나기 전에는 추적 손절이 작동하지 않습니다. 너무 일찍 발동하면 작은 흔들림에도 팔립니다.",
        },
        {
          path: "risk.dynamic_take_profit", label: "추세 강하면 익절폭 자동 확대", kind: "bool",
          hint: "켜면 추세 세기(ADX)가 강할 때만 익절 목표를 넓혀 크게 먹습니다(확대 기준·배수는 아래 '공통' 그룹에서 조절). 끄면 이 시장은 위에 정한 익절 폭을 그대로 씁니다.",
        },
        { path: "risk.technique_learning_mode", label: "매매 모델", kind: "select",
          options: [
            ["none", "기본 단타 룰만 (과거 실적·백테스트 가산점 없음)"],
            ["entry_pref", "진입 가산점 반영 (기본값 - 최근 시세 백테스트로 잘 맞은 기법 우대)"],
            ["entry_exit_pref", "진입+청산 가산점 반영 (보유 중 놓친 이익까지 추적해 다음 진입 때 익절 목표 확대)"],
          ],
          hint: "이 시장의 매매 결정에 과거 데이터를 얼마나 반영할지 고릅니다. entry_exit_pref 는 종목별 청산 효율 기록이 최소 3건 쌓여야 실제로 반영되기 시작합니다." },
        {
          path: "risk.daily_loss_limit_pct", label: "일일 손실 한도", kind: "pct", min: 0.5, max: 50, step: 0.5, suffix: "%",
          hint: "손실이 난 날 더 하려는 충동을 막는 장치입니다. 한도에 닿으면 오늘은 신규 진입을 멈춥니다.",
        },
        {
          path: "risk.weekly_loss_limit_pct", label: "주간 손실 한도", kind: "pct", min: 1, max: 50, step: 0.5, suffix: "%",
          hint: "이번 주는 더 하지 않고 다음 주에 다시 시작하게 만드는 장치입니다.",
        },
        {
          path: "risk.daily_max_trades", label: "일일 최대 거래 횟수", kind: "num", min: 1, max: 500, step: 1,
          hint: "과매매는 비용만으로 계좌를 갉아먹습니다. 하루 매매 건수를 미리 못박아 둡니다(기본 50회).",
        },
        {
          path: "risk.max_consecutive_losses", label: "연속 손절 한도", kind: "num", min: 1, max: 10, step: 1,
          hint: "이 횟수만큼 연속으로 손절하면 시장과 안 맞는다고 보고 멈추거나 규모를 줄입니다.",
        },
        {
          path: "risk.loss_halt_cooldown_hours", label: "연속 손절 후 재개 간격(시간)", kind: "num",
          min: 1, max: 168, step: 1,
          hint: "★ 연속 손절로 멈춘 뒤 이 시간이 지나거나, 그 전이라도 장의 세션(프리장·본장·NXT장)이 "
            + "바뀌면 신규 매수를 자동으로 재개합니다(마지막 손절 시각 기준). 재개 예정 시각은 대시보드에 "
            + "표시됩니다. 3시간이 기본입니다. 짧게 잡으면 손실을 만회하려는 매매로 이어지기 쉬우니 신중히 정하세요.",
        },
        {
          path: "risk.reduce_after_loss", label: "손실 뒤 크기 줄이기", kind: "bool",
          hint: "잃고 있을 때 크기를 키우는 건 물타기이고 계좌가 가장 빨리 망가지는 길입니다. 켜두길 권합니다.",
        },
        {
          path: "risk.reduced_size_pct", label: "축소 비율", kind: "pct", min: 10, max: 100, step: 5, suffix: "%",
          hint: "연속 손절 뒤 정상 대비 몇 %로 매매 규모를 줄일지입니다.",
        },
        {
          path: "risk.max_slippage_pct", label: "최대 슬리피지", kind: "pct", min: 0.05, max: 5, step: 0.05, suffix: "%",
          hint: "예상가보다 이 이상 불리하게 밀리면 주문 자체를 포기합니다.",
        },
        {
          path: "risk.cooldown_minutes", label: "재진입 금지 시간", kind: "num", min: 0, max: 120, step: 5, suffix: "분",
          hint: "손절한 종목에 감정적으로 바로 다시 들어가는 것을 막습니다.",
        },
        {
          path: "risk.min_order_amount", label: "최소 주문 금액", kind: "won", step: 1000,
          hint: "너무 작은 주문은 수수료 비중이 커져서 실행하지 않습니다.",
        },
      ],
    },
    {
      title: "비용", group: "국내주식",
      note: "암호화폐 매매에는 이 수수료·세율이 적용되지 않습니다 - 빗썸 수수료는 암호화폐 브로커 안에서 따로 계산됩니다.",
      fields: [
        { path: "costs.commission_pct", label: "수수료율", kind: "pct", min: 0, max: 1, step: 0.001, suffix: "%",
          hint: "매수·매도 각각 붙습니다. 증권사 수수료 안내를 그대로 옮기세요." },
        { path: "costs.tax_pct", label: "거래세율", kind: "pct", min: 0, max: 1, step: 0.01, suffix: "%",
          hint: "매도 시에만 붙습니다. 세율이 바뀌면 여기도 같이 고쳐야 손익 계산이 맞습니다." },
      ],
    },
    {
      title: "종목 선별", group: "국내주식",
      fields: [
        { path: "screen.ranking_count", label: "1차 후보 수", kind: "num", min: 10, max: 300, step: 10,
          hint: "테마 스코어링 전에 몇 종목까지 훑어볼지입니다. 너무 크면 느려집니다." },
        { path: "screen.min_trading_amount", label: "최소 거래대금", kind: "won", step: 100000000,
          hint: "유동성이 없는 종목은 사도 제때 못 팝니다." },
        { path: "screen.min_price", label: "최소 가격", kind: "won", step: 100,
          hint: "너무 싼 종목은 호가 하나 차이가 등락률에 크게 영향을 줍니다." },
        { path: "screen.max_price", label: "최대 가격", kind: "won", step: 1000,
          hint: "너무 비싼 종목은 1주 단위 리스크가 커집니다." },
        { path: "screen.min_change_rate", label: "최소 등락률", kind: "pct", min: 0, max: 30, step: 0.5, suffix: "%",
          hint: "움직임이 없는 종목은 애초에 신호가 나오지 않습니다." },
        { path: "screen.max_change_rate", label: "최대 등락률", kind: "pct", min: 1, max: 50, step: 1, suffix: "%",
          hint: "이미 너무 오른 종목은 추격매수가 되어 리스크가 큽니다(매매원칙 '추격매수 금지'). 시뮬레이션 비교에서 20%보다 12%가 승률·손익비가 좋았습니다." },
        { path: "screen.exclude_warnings", label: "제외할 거래소 경고", kind: "checklist",
          options: [
            ["LIQUIDATION_TRADING", "정리매매"],
            ["INVESTMENT_WARNING", "투자경고"],
            ["INVESTMENT_RISK", "투자위험"],
            ["OVERHEATED", "단기과열"],
          ],
          hint: "거래소가 지정한 위험 종목은 제도적 리스크가 있어 원천 제외합니다." },
        { path: "screen.exclude_preferred", label: "우선주 제외", kind: "bool",
          hint: "우선주는 유동성이 낮고 가격 왜곡이 흔합니다." },
        { path: "screen.exclude_etf_etn", label: "ETF/ETN 제외", kind: "bool",
          hint: "ETF/ETN 은 개별 종목 기법과 성격이 달라 대상에서 뺍니다." },
        { path: "screen.top_themes", label: "상위 테마 수", kind: "num", min: 1, max: 5, step: 1,
          hint: "오늘 인정된 테마 중 상위 몇 개까지 매매 대상으로 삼을지입니다." },
        { path: "screen.candidates_per_theme", label: "테마당 후보 수", kind: "num", min: 1, max: 5, step: 1,
          hint: "한 테마에 쏠려서 분산이 안 되는 것을 막습니다." },
        { path: "screen.min_theme_members_up", label: "테마 인정 최소 동반상승 종목 수", kind: "num", min: 1, max: 10, step: 1,
          hint: "한 종목만 튀는 건 테마가 아니라 개별 이슈로 봅니다." },
        { path: "screen.allow_unmapped", label: "미분류 테마 허용", kind: "bool",
          hint: "themes.yaml 에 없는 테마까지 허용하면 원칙 9(종목은 프로그램이 뽑고 과정을 보여준다)가 흐려집니다." },
        { path: "screen.auto_daily", label: "매일 자동 스크리닝", kind: "bool",
          hint: "꺼두면 매일 직접 눌러줘야 스크리닝이 돕니다." },
        { path: "screen.auto_time", label: "자동 스크리닝 시각", kind: "time",
          hint: "개장 직후 호가 왜곡이 가라앉는 시점 이후로 잡는 것을 권합니다." },
        { path: "screen.meta_cache_minutes", label: "종목 메타 캐시 시간", kind: "num", min: 5, max: 120, step: 5, suffix: "분",
          hint: "너무 짧으면 API 호출이 늘고, 너무 길면 상장폐지·종목명 변경이 늦게 반영됩니다." },
        { path: "screen.watchlist", label: "관심 종목(검색해서 추가)", kind: "searchlist", market: "domestic",
          placeholder: "종목명 또는 코드로 검색",
          hint: "★ 여기에 넣은 종목은 테마와 상관없이 항상 거래 대상 후보에 함께 들어갑니다. "
            + "거래정지·경고 종목만 안전상 제외되고, 실제 매수는 매매 기법 판정을 통과할 때만 합니다." },
      ],
    },
    {
      title: "진입", group: "국내주식",
      fields: [
        { path: "entry.extra_windows", label: "장 초반·막판 변동성 시간대 사용", kind: "bool",
          hint: "켜면 장 초반(개장~스캔 시작)과 장 막판(스캔 종료~막판 종료)에 그 시간대 전용 기법(시초 갭 돌파·장 막판 상승 지속)만 새로 살 수 있습니다. 두 기법은 진입 기법에서 켜야 작동합니다(빠른 단타 모드는 자동으로 켭니다). 프리마켓(NXT)은 지원하지 않습니다." },
        { path: "entry.open_window_start", label: "장 초반 시작 시각", kind: "time",
          hint: "기본 09:00. 스캔 시작 시각 이전이어야 합니다." },
        { path: "entry.close_window_end", label: "장 막판 종료 시각", kind: "time",
          hint: "기본 15:00. 스캔 종료 시각 이후, 강제 청산 시각 이전이어야 합니다(마감 전 10분은 청산에 씁니다)." },
        { path: "entry.selection_day_start", label: "종목 선정 시작 시각", kind: "time",
          hint: "기본 08:00. 매수 가능 시간과는 별개로, 국내 시장이 프리마켓·정규장·NXT 어떤 형태로든 열려 있다고 볼 하루 시작 시각입니다 - 이 시각 전에는 종목 선정을 아예 돌리지 않습니다(랭킹 API 호출 낭비 방지). 이 값을 바꿔도 매수 가능 시간대(위 스캔 시작·종료)는 그대로입니다." },
        { path: "entry.selection_day_end", label: "종목 선정 종료 시각", kind: "time",
          hint: "기본 21:00(NXT 마감 기준). 이 시각이 지나면 다음날 종목 선정 시작 시각까지 종목 선정을 돌리지 않습니다. 주말·공휴일에는 시각과 무관하게 항상 쉽니다." },
        { path: "entry.scan_start", label: "스캔 시작 시각", kind: "time",
          hint: "개장 직후 09:00~09:20 구간은 호가 왜곡이 커서 보통 피합니다." },
        { path: "entry.scan_end", label: "스캔 종료 시각", kind: "time",
          hint: "장 막판 변동성 구간은 신규 진입 대상에서 빼는 것을 권합니다." },
        { path: "entry.breakout_lookback", label: "돌파 판단 봉 수", kind: "num", min: 5, max: 60, step: 1,
          hint: "몇 봉 전 고가를 기준으로 돌파를 판정할지입니다." },
        { path: "entry.volume_surge_ratio", label: "거래량 급증 배수", kind: "num", min: 1, max: 10, step: 0.1,
          hint: "평균 대비 이 배수를 넘어야 진짜 수급이 붙었다고 봅니다." },
        { path: "entry.volume_window", label: "평균 거래량 기준 봉 수", kind: "num", min: 5, max: 60, step: 1,
          hint: "거래량 급증 여부를 비교할 평균을 몇 봉으로 낼지입니다." },
        { path: "entry.poll_seconds", label: "시세 조회 주기", kind: "num", min: 5, max: 120, step: 5, suffix: "초",
          hint: "너무 짧으면 API 한도를 빨리 태웁니다." },
        { path: "entry.rescreen_minutes", label: "재스크리닝 주기", kind: "num", min: 5, max: 60, step: 5, suffix: "분",
          hint: "장중 테마 변화를 얼마나 자주 반영할지입니다." },
        { path: "entry.max_vi_gap_pct", label: "상한가 근접 회피폭", kind: "pct", min: 0.5, max: 10, step: 0.5, suffix: "%",
          hint: "변동성완화장치(VI) 발동 직전에 물리는 것을 막습니다." },
        { path: "entry.use_closed_bars_only", label: "확정된 봉만 사용", kind: "bool",
          hint: "진행 중인 봉은 거래량이 부분값이라 판정이 왜곡됩니다. 끄지 않는 것을 권합니다." },
      ],
    },
    {
      title: "청산", group: "국내주식",
      fields: [
        { path: "exit.force_close_time", label: "강제 청산 시각", kind: "time",
          hint: "동시호가 혼란을 피하려면 마감 전에 정리해야 합니다." },
        { path: "exit.force_close_deadline_min", label: "강제 청산 유예 시간", kind: "num", min: 1, max: 20, step: 1, suffix: "분",
          hint: "강제 청산 시도가 계속 실패할 때 얼마나 더 시도한 뒤 포기하고 알릴지입니다." },
        { path: "exit.use_conditional_oco", label: "서버 OCO 사용", kind: "bool",
          hint: "켜면 손절·익절 주문이 증권사 서버에도 등록됩니다. 프로그램이 꺼져도 손절이 살아있습니다." },
        { path: "exit.max_hold_minutes", label: "최대 보유 시간", kind: "num", min: 5, max: 300, step: 5, suffix: "분",
          hint: "데이트레이딩 원칙상 포지션을 오래 끌지 않기 위한 상한입니다." },
        { path: "exit.allow_overnight", label: "이익 중인 포지션 오버나이트 허용", kind: "bool",
          hint: "켜면 장 마감 시각에도 무조건 청산하지 않고, 아래 조건을 만족하는 '이익 중인' 포지션만 "
            + "다음 거래일로 최대 1일 넘깁니다. 조건에 못 미치는 포지션과 손실 중인 포지션은 이익 여부와 "
            + "무관하게 그대로 당일 청산됩니다. 끄면 예전처럼 장 마감에 전부 청산합니다." },
        { path: "exit.overnight_min_profit_pct", label: "오버나이트 최소 수익 기준", kind: "pct", min: 0, max: 50, step: 0.5, suffix: "%",
          showIf: { path: "exit.allow_overnight", equals: true },
          hint: "장 마감 시점 평가손익이 수수료·거래세를 뺀 뒤에도 이 비율 이상이어야 넘깁니다. "
            + "못 미치면(손실 포함) 그날 안에 팝니다. 기본 2%." },
        { path: "exit.overnight_max_days", label: "최대 연속 오버나이트 일수", kind: "num", min: 1, max: 1, step: 1, suffix: "일",
          showIf: { path: "exit.allow_overnight", equals: true },
          hint: "한 번 넘긴 포지션을 다시 넘길 수 있는 최대 일수입니다. 지금은 1일만 지원합니다 - "
            + "그 이상 연속으로 들고 가는 것은 데이트레이딩이 아니라 스윙 매매의 영역이라 이 엔진에서는 다루지 않습니다." },
        { path: "exit.overnight_skip_before_holiday", label: "휴장 전날은 연장 안 함", kind: "bool",
          showIf: { path: "exit.allow_overnight", equals: true },
          hint: "다음 거래일이 내일이 아닌 날(주말·공휴일 앞 마지막 거래일)에는 이익이 충분해도 넘기지 않고 "
            + "당일 청산합니다 - 쉬는 날 동안 뉴스·급락에 그대로 노출되는 기간을 줄입니다." },
        { path: "exit.overnight_breakeven_stop", label: "연장 시 손절선을 본전으로 올림", kind: "bool",
          showIf: { path: "exit.allow_overnight", equals: true },
          hint: "넘기기로 한 포지션은 손절선을 평균 매수가(본전, 비용 포함)로 올리고 서버 OCO를 그 값으로 "
            + "다시 겁니다 - 이익 중이던 포지션이 다음날 손실로 마감되는 것을 막습니다. 재설정이 실패하면 "
            + "안전을 위해 넘기지 않고 그 자리에서 청산합니다. 끄면 손절선은 원래 값 그대로 두고 수량만 넘깁니다." },
      ],
    },
    {
      title: "매매 기법", group: "공통",
      note: "켜진 기법 중에서 지금 무엇을 쓸지는 아래 '자동 선정'이 정합니다. 자세한 동작은 [매매 기법] 화면을 보세요.",
      fields: [
        { path: "strategy.entry_order", label: "진입 기법", kind: "techlist", which: "entry",
          hint: "여기서 체크한 기법들이 '후보'가 됩니다. 그중 실제로 무엇을 쓸지는 자동 선정이 매 순간 정합니다." },
        { path: "strategy.exit_enabled", label: "청산 기법", kind: "techlist", which: "exit",
          hint: "고정 손절·익절(fixed)은 항상 켜져 있어야 합니다 - 손절 없는 매매는 없습니다." },
      ],
    },
    {
      title: "자동 선정", group: "공통",
      note: "여러 종목·기법이 동시에 조건을 만족할 때, 무엇을 고를지 정하는 규칙입니다.",
      fields: [
        { path: "strategy.best_signal", label: "신호가 가장 강한 것 선택", kind: "bool",
          hint: "★ 켜면(권장) 후보를 전부 평가한 뒤 신호가 가장 강한 종목·기법을 고릅니다. "
            + "끄면 예전 방식대로 '목록 순서대로 처음 통과한 것'을 씁니다 - 목록 앞쪽이라는 이유만으로 "
            + "약한 신호를 사서 거래 한도를 소진할 수 있습니다." },
        { path: "strategy.performance_weight", label: "과거 실적 반영 강도", kind: "pct",
          hint: "★ 실적이 좋았던 기법의 신호 점수에 가산점을 줍니다. 0%면 순수 신호 강도만 봅니다. "
            + "기본 30% - 신호 강도가 여전히 주된 기준이고 실적은 보조입니다. "
            + "너무 높이면 과거에 우연히 잘 맞았던 기법에 쏠릴 수 있어 권하지 않습니다." },
        { path: "strategy.min_trades_for_weight", label: "실적 반영 최소 거래 수", kind: "num",
          hint: "★ 이 건수 미만인 기법은 실적을 반영하지 않습니다. 표본이 적으면 승률이 운에 크게 "
            + "좌우되기 때문입니다 - 낮출수록 검증 안 된 기법에 과적합될 위험이 커집니다." },
      ],
    },
    {
      title: "속보", group: "정보·알림",
      fields: [
        { path: "news.mode", label: "매매 개입 방식", kind: "select",
          options: [
            ["off", "꺼짐 - 속보를 보지 않습니다"],
            ["view", "참고용 표시만 (권장)"],
            ["avoid", "위험 신호가 있으면 후보에서 제외"],
            ["boost", "테마 점수에 가점 반영 (권장하지 않음)"],
          ],
          hint: "이 모듈은 제목의 문자열만 봅니다. '급락'과 '급등'을 구분하지 못하니 boost 는 신중히 쓰세요." },
        { path: "news.risk_hours", label: "위험 신호 유효 시간", kind: "num", min: 1, max: 72, step: 1, suffix: "시간",
          hint: "이 시간 안의 위험어 포함 기사만 avoid 판단에 씁니다." },
        { path: "news.ai_filter", label: "AI 뉴스·공시 위험 필터", kind: "bool",
          hint: "Groq 키가 등록돼 있을 때만 작동합니다. 사기 직전에 종목의 최근 헤드라인을 AI 가 읽고 악재(상장폐지·횡령·유상증자·소송·해킹 등)가 있으면 사지 않습니다. 거르기 전용이며, 실패하면 통과합니다." },
        { path: "news.groq_model", label: "Groq 모델", kind: "select",
          options: [["openai/gpt-oss-20b", "GPT-OSS 20B(빠름 - 권장)"], ["openai/gpt-oss-120b", "GPT-OSS 120B(더 정확, 더 느림)"]],
          hint: "Groq 가 제공하는 모델은 자주 바뀝니다 - 이 필터가 갑자기 계속 실패하면(연결 테스트로 확인) console.groq.com/docs/models 에서 지금 쓸 수 있는 모델 이름으로 바꿔 등록하세요." },
        { path: "news.ai_max_calls_per_day", label: "AI 하루 호출 상한", kind: "num", min: 0, max: 5000, step: 50,
          hint: "넘으면 그날은 AI 없이(키워드 제외 모드만) 돌립니다. 0 이면 제한 없음. 종목당 60분 캐시가 있어 보통 수십 회 수준입니다." },
        { path: "news.enabled", label: "속보 수집 사용", kind: "bool",
          hint: "꺼두면 속보 화면 자체가 비어 있습니다." },
        { path: "news.theme_queries", label: "테마별 검색 사용", kind: "bool",
          hint: "themes.yaml 의 테마명으로도 뉴스를 검색해 테마 흐름을 함께 봅니다." },
        { path: "news.lookback_hours", label: "조회 기간", kind: "num", min: 1, max: 72, step: 1, suffix: "시간",
          hint: "화면에 보여줄 기사의 최대 과거 범위입니다." },
        { path: "news.refresh_minutes", label: "새로고침 주기", kind: "num", min: 1, max: 60, step: 1, suffix: "분",
          hint: "너무 짧으면 소스 서버에 부담을 줍니다." },
        { path: "news.auto_refresh_seconds", label: "화면 자동 갱신 주기", kind: "num", min: 10, max: 300, step: 10, suffix: "초",
          hint: "화면이 얼마나 자주 새로고침되는지입니다." },
        { path: "news.reaction_window_min", label: "반응 관찰 시간창", kind: "num", min: 5, max: 120, step: 5, suffix: "분",
          hint: "뉴스 발생 후 시장 반응을 관찰할 시간 범위입니다. 상관일 뿐 인과가 아님을 기억하세요." },
        { path: "news.theme_boost", label: "테마 점수 가점", kind: "num", min: 0, max: 1, step: 0.05,
          hint: "0이면 매매 판단에 전혀 개입하지 않습니다 (권장)." },
      ],
    },
    {
      title: "알림", group: "정보·알림",
      note: "[준비·연결] → 외부 연동 에서 토큰·채팅ID 를 넣습니다.",
      fields: [
        { path: "notify.enabled", label: "알림 사용", kind: "bool",
          hint: "꺼두면 매매·일일·월간·연간 알림이 전혀 나가지 않습니다." },
        { path: "notify.daily_review", label: "일일 매매 복기 보내기", kind: "bool",
          hint: "장이 끝난 뒤 그날의 매매를 정리해 텔레그램으로 보냅니다: 국내는 마감 10분 뒤(15:40), 미국은 한국시간 아침 06:00(직전 미국 거래일), 암호화폐는 매일 21:00. 그날 거래·보유가 없으면 보내지 않습니다." },
        { path: "notify.overseas_review_time", label: "해외 복기 시각(한국시간)", kind: "time", hint: "기본 06:00." },
        { path: "notify.crypto_review_time", label: "암호화폐 복기 시각", kind: "time", hint: "기본 21:00. 직전 24시간을 복기합니다." },
        { path: "notify.crypto_review_time2", label: "암호화폐 복기 시각 2(선택)", kind: "time",
          hint: "24시간 시장이라 하루 한 번으로는 뜸하다면 여기에 두 번째 시각을 넣으세요(예: 09:00). 비워두면 하루 한 번만 보냅니다." },
        { path: "notify.weekly_review_enabled", label: "주간 복기 보내기(전체 시장)", kind: "bool",
          hint: "국내·해외·암호화폐·스윙 4개 시장을 한 메시지에 묶어 지난 7일을 복기해 보냅니다. 통화 단위가 달라(원/달러) 하나로 합산하지 않고 시장별로 줄을 나눠 보여줍니다." },
        { path: "notify.weekly_review_day", label: "주간 복기 요일", kind: "select",
          options: [["mon", "월요일"], ["tue", "화요일"], ["wed", "수요일"], ["thu", "목요일"], ["fri", "금요일"], ["sat", "토요일"], ["sun", "일요일"]],
          hint: "이 요일에 지난 7일치를 복기해 보냅니다." },
        { path: "notify.weekly_review_time", label: "주간 복기 시각", kind: "time", hint: "기본 08:00." },
        { path: "notify.monthly_review_enabled", label: "월간 복기 보내기(전체 시장)", kind: "bool",
          hint: "국내·해외·암호화폐·스윙 4개 시장을 한 메시지에 묶어 지난 달 전체를 복기해 보냅니다." },
        { path: "notify.monthly_review_day", label: "월간 복기 날짜", kind: "num", min: 1, max: 28, step: 1,
          hint: "매달 이 날짜에 지난 달 전체를 복기해 보냅니다(모든 달에 있는 1~28일만 선택 가능)." },
        { path: "notify.monthly_review_time", label: "월간 복기 시각", kind: "time", hint: "기본 08:00." },
        { path: "notify.market_review_enabled", label: "오늘의 시장 평가 보내기(AI 요약)", kind: "bool",
          hint: "★ 내 매매 복기와는 별개입니다 - 국내는 본장 마감 후, 해외는 뉴욕 마감 후(아래 '해외 복기 시각'과 같은 시각) 실제 지수·주가(해외만)와 관련 뉴스(국내는 증권사 리포트·수급, 해외는 블룸버그·마켓워치 등 영어권 소스 중심으로 기술주·AI 반도체 동향에 집중)를 모아 Groq 로 요약해 보냅니다. "
            + "Groq 키가 등록되어 있어야 실제로 보내집니다(아래 '속보' 섹션에서 등록) - 없으면 조용히 건너뜁니다. 보낸 내용은 [매매일지] 근처의 '시장 평가' 화면에서 날짜별로 다시 볼 수 있습니다. AI가 실제 시세·뉴스를 근거로 정리한 참고용 요약이며 투자 조언이 아닙니다." },
        { path: "notify.market_review_time", label: "국내 시장 평가 발송 시각", kind: "time",
          hint: "기본 15:40(국내 본장 마감 10분 뒤 - 매매 복기와 같은 시각). 해외 시장 평가는 위 '해외 복기 시각'을 그대로 씁니다." },
        { path: "notify.notify_in_practice", label: "연습 모드에서도 알림 보내기", kind: "bool",
          hint: "★ 기본은 실거래에서만 알림을 보냅니다 - 연습 매매 알림이 실제 주문과 섞이면 "
            + "위험하기 때문입니다. 모의매매·시뮬레이션 중에도 알림으로 확인하고 싶으면 켜세요. "
            + "이때 메시지 앞에 🧪 [연습] 표시가 붙어 실거래와 구분됩니다." },
        { path: "notify.events", label: "알림 보낼 이벤트", kind: "checklist",
          options: [
            ["trade", "매매(진입·청산)"],
            ["daily", "일일 요약"],
            ["monthly", "월간 요약"],
            ["yearly", "연간 요약"],
          ],
          hint: "실거래에서만 나갑니다. 연습(sim/paper) 중에는 알림이 울리지 않습니다." },
      ],
    },
    {
      title: "암호화폐(빗썸)", group: "암호화폐",
      note: "국내주식과 완전히 분리된 설정입니다 - 위 '거래할 시장'에서 켜야 동작합니다. [준비·연결]에서 빗썸 키를 등록하지 않으면 모의매매로만 동작합니다.",
      fields: [
        { path: "crypto.mode", label: "암호화폐 거래 모드", kind: "select",
          options: [
            ["web", "관찰(web) - 신호만 기록하고 실제로 사지 않습니다"],
            ["sim", "시뮬레이션(sim) - 지금은 모의매매와 동일하게 동작합니다(합성 데이터 시뮬레이터 준비 중)"],
            ["paper", "모의매매(paper) - 실시간 시세로 가짜 주문을 냅니다"],
            ["live", "실거래(live) - 실제 빗썸 계좌로 진짜 주문을 냅니다"],
          ],
          hint: "★ 국내주식의 동작 모드와 완전히 별개입니다. 국내주식을 실거래로 돌려도 이걸 live로 안 바꾸면 암호화폐는 그대로 관찰·모의매매로 남습니다." },
        { path: "crypto.entry_order", label: "진입 기법", kind: "techlist", which: "crypto_entry",
          hint: "여러 개를 켜면 위에서부터 순서대로 확인해 처음 통과한 것을 씁니다. [매매 기법] 화면에서 원전을 볼 수 있습니다." },
        { path: "crypto.exit_enabled", label: "청산 기법", kind: "techlist", which: "crypto_exit",
          hint: "손절은 항상 가장 먼저 확인합니다 - 꺼도 되지만 권장하지 않습니다." },
        { path: "crypto.watchlist", label: "거래 대상 암호화폐(검색해서 추가)", kind: "searchlist", market: "crypto",
          placeholder: "코인명 또는 마켓 코드로 검색",
          hint: "빗썸 전체 마켓에서 이름·코드로 검색해 추가합니다(저장되는 값은 예전과 같은 마켓 코드, 예: KRW-BTC)." },
        { path: "crypto.auto_top_volume", label: "전날 거래대금 상위 종목만 감시", kind: "bool",
          hint: "켜면 매일 빗썸 KRW 마켓 중 전날 거래대금 상위 종목(스테이블코인 제외)만 감시합니다. 위 '거래 대상 암호화폐'는 이 선정에 실패했거나 끄면 쓰는 기본 목록입니다. 보유 중인 코인은 목록에서 빠져도 끝까지 관리합니다." },
        { path: "crypto.top_volume_count", label: "거래대금 상위 종목 수", kind: "num", min: 1, max: 50, step: 1,
          hint: "전날 거래대금이 큰 순서로 몇 종목을 감시할지입니다(기본 10)." },
        { path: "crypto.reentry_cooldown_minutes", label: "손절 후 같은 코인 재진입 대기(분)", kind: "num", min: 0, max: 720, step: 5,
          hint: "★ 손절 직후 30분 안에 같은 코인을 다시 산 10건이 전부 손실이었던 실제 이력에서 만든 규칙입니다. 0이면 끕니다." },
        { path: "crypto.k", label: "변동성 돌파 계수(k)", kind: "num", min: 0.1, max: 1.0, step: 0.01,
          hint: "표준 0.5. 낮추면 신호가 잦아지고(가짜 돌파 증가), 높이면 신호가 드물어집니다." },
        { path: "crypto.min_trading_value_krw", label: "최소 거래대금(원)", kind: "won", step: 1000000,
          hint: "★ \"암호화폐는 거래량이 적은 경우 거래를 하지 않도록 보완해\" 요청. 최근 봉 평균 거래대금(종가×거래량)이 이 밑이면 매수하지 않습니다 - 유동성이 얇으면 호가 간격이 넓어 슬리피지가 크고 소량 체결에도 가격이 쉽게 튑니다. 0이면 끕니다(기본 500만원)." },
        { path: "crypto.min_trading_value_bars", label: "거래대금 평균 봉 수", kind: "num", min: 1, max: 60, step: 1,
          hint: "위 최소 거래대금을 최근 몇 개 봉의 평균으로 볼지입니다(기본 10개) - 봉 하나만 보면 우연히 거래가 뜸했을 뿐인데 오판할 수 있습니다." },
        { path: "crypto.stop_loss_pct", label: "손절", kind: "pct",
          hint: "암호화폐는 변동성이 커서 국내주식보다 넓게 잡습니다(기본 5%)." },
        { path: "crypto.take_profit_pct", label: "익절", kind: "pct" },
        { path: "crypto.trailing_pct", label: "추적 청산(고점 대비 하락)", kind: "pct" },
        { path: "crypto.dynamic_take_profit", label: "추세 강하면 익절폭 자동 확대", kind: "bool",
          hint: "켜면 추세 세기(ADX)가 강할 때만 익절 목표를 넓혀 크게 먹습니다(확대 기준·배수는 '공통' 그룹). 끄면 위 익절 폭을 그대로 씁니다." },
        { path: "crypto.technique_learning_mode", label: "매매 모델", kind: "select",
          options: [
            ["none", "기본 단타 룰만 (과거 실적·백테스트 가산점 없음)"],
            ["entry_pref", "진입 가산점 반영 (기본값 - 최근 시세 백테스트로 잘 맞은 기법 우대)"],
            ["entry_exit_pref", "진입+청산 가산점 반영 (보유 중 놓친 이익까지 추적해 다음 진입 때 익절 목표 확대)"],
          ],
          hint: "이 시장의 매매 결정에 과거 데이터를 얼마나 반영할지 고릅니다. entry_exit_pref 는 종목별 청산 효율 기록이 최소 3건 쌓여야 실제로 반영되기 시작합니다." },
        { path: "crypto.max_hold_hours", label: "최대 보유 시간", kind: "num",
          hint: "장 마감이 없는 24시간 시장이라 '하루'를 시간으로 대신합니다." },
        { path: "crypto.daily_loss_limit_pct", label: "24시간 손실 한도", kind: "pct",
          hint: "직전 24시간 손실이 이 비율을 넘으면 신규 진입을 멈춥니다(이미 보유 중인 것의 청산은 계속됩니다)." },
        { path: "crypto.daily_max_trades", label: "일일 최대 거래 횟수", kind: "num", min: 1, max: 500, step: 1,
          hint: "과매매는 비용만으로 계좌를 갉아먹습니다. 직전 24시간 매매 건수가 이 값에 닿으면 신규 진입을 멈춥니다(기본 50회)." },
        { path: "crypto.consecutive_loss_halt", label: "연속 손절 중단 횟수", kind: "num",
          hint: "연속으로 이 횟수만큼 손절하면 신규 진입을 멈춥니다." },
        { path: "crypto.loss_halt_cooldown_hours", label: "연속 손절 후 재개 간격(시간)", kind: "num",
          min: 1, max: 168, step: 1,
          hint: "★ 연속 손절로 멈춘 뒤 이 시간이 지나면 신규 매수를 자동으로 재개합니다(마지막 손절 시각 기준). "
            + "재개 예정 시각은 대시보드에 표시됩니다. 3시간이 기본입니다. 암호화폐는 24시간 시장이라 "
            + "세션 구분이 없어 시간 경과만 봅니다." },
        { path: "crypto.poll_seconds", label: "판단 주기(초)", kind: "num",
          hint: "이 주기마다 시세를 다시 확인해 매수·매도를 판단합니다." },
        { path: "crypto.commission_pct", label: "거래 수수료", kind: "pct",
          hint: "모의매매 손익 계산에 반영합니다(빗썸 편도 수수료 근사치, 기본 0.04%). 실거래 수수료는 거래소 정책을 따릅니다." },
      ],
    },
    {
      title: "해외주식", group: "해외주식",
      note: "국내주식과 같은 매매 기법·같은 계좌를 씁니다(위 '국내주식' 탭의 진입·청산 '기법 선택'을 그대로 따릅니다). "
        + "손절·익절·추적청산 폭·최대 보유시간은 아래에서 따로 정합니다(원화·달러, 변동성이 다른 시장이라 국내와 분리 - "
        + "기본값은 국내와 동일하게 시작합니다). 그 외 다른 것은 종목(테마 없이 아래 관심종목만 봄)·실적 기록·미국 정규장 시간입니다.",
      fields: [
        { path: "overseas.stop_loss_pct", label: "손절", kind: "pct",
          hint: "기본값은 국내주식과 동일(3.5%)로 시작합니다. 필요하면 따로 조정하세요." },
        { path: "overseas.take_profit_pct", label: "익절", kind: "pct" },
        { path: "overseas.trailing_pct", label: "추적 손절 하락폭", kind: "pct",
          hint: "고점을 찍은 뒤 이만큼 되밀리면 남은 수익을 지키기 위해 정리합니다." },
        { path: "overseas.trailing_arm_pct", label: "추적 손절 발동 기준", kind: "pct",
          hint: "수익이 이만큼 나기 전에는 추적 손절이 작동하지 않습니다." },
        { path: "overseas.dynamic_take_profit", label: "추세 강하면 익절폭 자동 확대", kind: "bool",
          hint: "켜면 추세 세기(ADX)가 강할 때만 익절 목표를 넓혀 크게 먹습니다(확대 기준·배수는 '공통' 그룹). 끄면 위 익절 폭을 그대로 씁니다." },
        { path: "overseas.technique_learning_mode", label: "매매 모델", kind: "select",
          options: [
            ["none", "기본 단타 룰만 (과거 실적·백테스트 가산점 없음)"],
            ["entry_pref", "진입 가산점 반영 (기본값 - 최근 시세 백테스트로 잘 맞은 기법 우대)"],
            ["entry_exit_pref", "진입+청산 가산점 반영 (보유 중 놓친 이익까지 추적해 다음 진입 때 익절 목표 확대)"],
          ],
          hint: "이 시장의 매매 결정에 과거 데이터를 얼마나 반영할지 고릅니다. entry_exit_pref 는 종목별 청산 효율 기록이 최소 3건 쌓여야 실제로 반영되기 시작합니다." },
        { path: "overseas.max_hold_minutes", label: "최대 보유 시간(분)", kind: "num", min: 10, step: 10,
          hint: "이 시간을 넘기면 시간 손절로 정리합니다(오버나이트 허용 시에도 적용)." },
        { path: "overseas.daily_max_trades", label: "일일 최대 거래 횟수", kind: "num", min: 1, max: 500, step: 1,
          hint: "과매매는 비용만으로 계좌를 갉아먹습니다. 하루 매매 건수를 미리 못박아 둡니다(기본 50회) - 국내주식과 별도 값입니다." },
        { path: "overseas.mode", label: "해외주식 거래 모드", kind: "select",
          options: [
            ["web", "관찰(web) - 신호만 기록하고 실제로 사지 않습니다"],
            ["sim", "시뮬레이션(sim) - 지금은 모의매매와 동일하게 동작합니다(합성 데이터 시뮬레이터 준비 중)"],
            ["paper", "모의매매(paper) - 실시간 시세로 가짜 주문을 냅니다"],
            ["live", "실거래(live) - 실제 계좌로 진짜 주문을 냅니다(국내주식과 같은 계좌)"],
          ],
          hint: "★ 국내주식의 거래 모드와 완전히 별개입니다. 국내주식을 실거래로 돌려도 이걸 live로 안 바꾸면 해외주식은 관찰·모의매매로 남습니다." },
        { path: "overseas.trade_premarket", label: "프리장 거래(뉴욕 04:00~09:30)", kind: "bool",
          hint: "★ \"본장·프리장·애프터장·데이장 등 장 별로 거래를 할지 선택하게 해\" 요청. 미국 주식은 평일 24시간 거래됩니다(일요일 저녁 8시~금요일 저녁 8시 뉴욕시각) - 4개 세션을 각각 켜고 끌 수 있습니다. 기본은 본장(정규장)만 켜져 있습니다(국내주식과 같은 원칙) - 시간외에는 호가 간격이 넓고 유동성이 얇아 슬리피지가 커질 수 있고, 시세가 15분 넘게 낡았으면 신규 진입을 건너뜁니다." },
        { path: "overseas.trade_regular", label: "본장(정규장) 거래(뉴욕 09:30~16:00)", kind: "bool",
          hint: "가장 유동성이 좋은 정규장입니다." },
        { path: "overseas.trade_afterhours", label: "애프터장 거래(뉴욕 16:00~20:00)", kind: "bool",
          hint: "정규장 마감 직후 시간외입니다." },
        { path: "overseas.trade_overnight", label: "데이장(야간거래) 거래(뉴욕 20:00~04:00)", kind: "bool",
          hint: "한국 시간으로는 낮 시간대입니다(주간거래)." },
        { path: "overseas.theme_select", label: "테마주 자동 산정", kind: "bool",
          hint: "★★★ 켜면 미국 종목을 테마(AI반도체·방산우주·원전 등)로 묶어, 오늘 한 테마의 종목들이 함께 오를 때 그 테마의 강한 종목을 자동으로 거래 대상에 넣습니다(국내주식과 같은 방식). 아래 관심 종목은 테마와 별개로 항상 함께 거래합니다. 테마 사전은 실행 파일 옆에 us_themes.yaml 을 두면 그것으로 바꿔 씁니다." },
        { path: "overseas.top_themes", label: "상위 테마 수", kind: "num", min: 1, max: 6, step: 1,
          hint: "오늘 인정된 미국 테마 중 상위 몇 개까지 거래 대상으로 삼을지입니다." },
        { path: "overseas.candidates_per_theme", label: "테마당 후보 수", kind: "num", min: 1, max: 6, step: 1,
          hint: "한 테마에 쏠리는 것을 막습니다." },
        { path: "overseas.min_theme_members_up", label: "테마 인정 최소 동반상승 종목 수", kind: "num", min: 1, max: 10, step: 1,
          hint: "한 종목만 튀는 건 테마가 아니라 개별 이슈로 봅니다." },
        { path: "overseas.theme_min_change_rate", label: "테마 상승 인정 등락률", kind: "pct", min: 0, max: 20, step: 0.5, suffix: "%",
          hint: "이 이상 오른 종목을 '동반 상승'으로 셉니다." },
        { path: "overseas.theme_max_change_rate", label: "최대 등락률(추격 제한)", kind: "pct", min: 1, max: 50, step: 1, suffix: "%",
          hint: "이보다 더 오른 종목은 추격매수라 후보에서 뺍니다." },
        { path: "overseas.theme_refresh_minutes", label: "테마 갱신 주기", kind: "num", min: 5, max: 720, step: 5, suffix: "분",
          hint: "테마와 후보를 이 간격으로 다시 평가합니다(실패하면 10분 뒤 재시도, 그동안은 직전 결과 유지)." },
        { path: "overseas.auto_select", label: "랭킹 자동 선정(추가)", kind: "bool",
          hint: "켜면 테마와 별개로 거래대금·급등 상위 종목도 거래 대상에 더합니다. 끄면 테마주와 관심 종목만 봅니다." },
        { path: "overseas.auto_select_count", label: "자동 선정 종목 수", kind: "num",
          hint: "종목 자동 선정을 켰을 때, 매일 몇 개 종목을 뽑을지 정합니다." },
        { path: "overseas.watchlist", label: "관심 종목(검색해서 추가)", kind: "searchlist", market: "overseas",
          placeholder: "종목명 또는 티커로 검색",
          hint: "기본값은 매그니피센트 7(AAPL·MSFT·GOOGL·AMZN·NVDA·META·TSLA)과 "
            + "AI반도체 대표주(AMD·AVGO·TSM·MU·ASML·ARM·SMCI·INTC)입니다. "
            + "★ 여기 넣은 종목은 테마주 자동 산정·랭킹 자동 선정과 별개로 항상 거래 대상에 함께 들어갑니다. "
            + "검색 목록은 자주 거래되는 대형주·ETF 위주라 모든 티커를 담고 있지는 않습니다(못 찾으면 잠시 후 다시 검색해 보세요)." },
        { path: "overseas.poll_seconds", label: "판단 주기(초)", kind: "num",
          hint: "미국 주식은 국내처럼 초 단위로 급하게 안 봅니다 - 기본 5분(300초)." },
        { path: "overseas.min_price", label: "최소 매수 가격($)", kind: "num", min: 0, step: 0.5,
          hint: "★★★ 이 가격 미만인 종목은 신호가 있어도 사지 않습니다. 종목 자동 선정이 그날 가장 많이 움직인 초저가 종목을 뽑는 경우가 있는데, 이런 종목은 급등락이 심해 고정 손절·익절과 상성이 나쁩니다." },
        { path: "overseas.max_daily_change_pct", label: "당일 과열 제외 기준", kind: "pct",
          hint: "전일 종가 대비 이 비율 넘게 이미 움직인 종목은 쫓아 사지 않습니다(0으로 두면 끔)." },
      ],
    },
    {
      title: "스윙(며칠~몇 주 보유)", group: "스윙",
      note: "국내주식·해외주식·암호화폐 세 시장을 하나의 스윙 엔진이 함께 감시·보유합니다(시장별로 따로 켜고 끄지 "
        + "않습니다) - 계좌는 각 시장의 실제 계좌(국내·해외는 같은 토스 계좌, 암호화폐는 빗썸)를 그대로 쓰되, 봉 "
        + "하나가 1분이 아니라 하루(일봉)인 전용 기법·설정을 씁니다. 위 '거래할 시장'에서 켜야 동작합니다. "
        + "투자금액은 세 시장 통화를 환산 없이 그대로 섞어 씁니다(대략적인 예산 관리용). "
        + "실거래(live)는 아직 지원하지 않습니다(관찰·모의매매만).",
      fields: [
        { path: "swing.mode", label: "스윙 거래 모드", kind: "select",
          options: [
            ["web", "관찰(web) - 신호만 기록하고 실제로 사지 않습니다"],
            ["paper", "모의매매(paper) - 실시간 시세로 가짜 주문을 냅니다"],
            ["live", "실거래(live) - 아직 지원하지 않습니다(시작 시 거부됩니다)"],
          ],
          hint: "★ 국내 단타의 거래 모드와 완전히 별개입니다." },
        { path: "swing.entry_order", label: "진입 기법", kind: "techlist", which: "swing_entry",
          hint: "일봉 전용 기법 3종(이동평균 눌림목·박스권 돌파·골든크로스)입니다. [매매 기법] 화면에서 원전을 볼 수 있습니다." },
        { path: "swing.exit_enabled", label: "청산 기법", kind: "techlist", which: "swing_exit",
          hint: "손절은 항상 가장 먼저 확인합니다 - 꺼도 되지만 권장하지 않습니다." },
        { path: "swing.watchlist", label: "스윙 전용 관심 종목 - 국내(검색해서 추가)", kind: "searchlist", market: "domestic",
          placeholder: "종목명 또는 코드로 검색",
          hint: "국내 단타 관심 종목과는 별개 목록입니다. "
            + "국내 테마 후보와 함께 아래 두 필터를 통과해야 실제 진입 후보가 됩니다." },
        { path: "swing.overseas_watchlist", label: "스윙 전용 관심 종목 - 해외(검색해서 추가)", kind: "searchlist", market: "overseas",
          placeholder: "종목명 또는 티커로 검색",
          hint: "해외주식 단타 관심 종목(overseas.watchlist)과 합쳐서 후보 풀로 씁니다." },
        { path: "swing.crypto_watchlist", label: "스윙 전용 관심 종목 - 암호화폐(검색해서 추가)", kind: "searchlist", market: "crypto",
          placeholder: "코인명 또는 마켓 코드로 검색",
          hint: "암호화폐 단타 관심 종목(crypto.watchlist)과 합쳐서 후보 풀로 씁니다. "
            + "[준비·연결]에 빗썸 키가 없어도 검색은 됩니다(공개 API)." },
        { path: "swing.trend_filter_ma", label: "추세 필터(이동평균 일수)", kind: "num", min: 5, max: 200, step: 5, suffix: "일",
          hint: "종가가 이 일수 이동평균 위(중기 상승 추세)인 종목만 스윙 후보로 인정합니다." },
        { path: "swing.week_momentum_min_pct", label: "최근 1주 실제 상승률 최소 기준", kind: "pct", min: -0.2, max: 0.3, step: 0.01,
          hint: "★★★ \"종목선정시 단타매매와는 다른 기법 사용하고, 최근 일주일 동안 실제 주가 변동에 따라 선정\" - "
            + "당일 실시간 등락률이 아니라, 확정된 지난 5거래일(약 1주일) 종가 변동률이 이 값 이상인 종목만 스윙 "
            + "후보로 남깁니다. 기본 0% = 최근 1주 하락한 종목은 제외." },
        { path: "swing.budget", label: "스윙 총 투자금액", kind: "num", min: 0, step: 100000, suffix: "원(달러·원화 혼합, 근사)",
          hint: "종목당 한도 = 이 금액 ÷ 동시 보유 한도. 해외(달러)·암호화폐(원화) 포지션도 환산 없이 같은 예산에서 나눠 씁니다." },
        { path: "swing.max_positions", label: "동시 보유 한도", kind: "num", min: 1, max: 20, step: 1,
          hint: "동시에 몇 종목까지 들고 갈지입니다." },
        { path: "swing.stop_loss_pct", label: "손절", kind: "pct",
          hint: "일봉 기준이라 국내 단타보다 훨씬 넓게 잡습니다(기본 8%) - 좁으면 정상적인 며칠치 변동에도 털립니다." },
        { path: "swing.take_profit_pct", label: "익절", kind: "pct" },
        { path: "swing.trailing_pct", label: "추적 청산(고점 대비 하락)", kind: "pct" },
        { path: "swing.dynamic_take_profit", label: "추세 강하면 익절폭 자동 확대", kind: "bool",
          hint: "켜면 추세 세기(ADX)가 강할 때만 익절 목표를 넓혀 크게 먹습니다(확대 기준·배수는 '공통' 그룹). 끄면 위 익절 폭을 그대로 씁니다." },
        { path: "swing.technique_learning_mode", label: "매매 모델", kind: "select",
          options: [
            ["none", "기본 단타 룰만 (과거 실적·백테스트 가산점 없음)"],
            ["entry_pref", "진입 가산점 반영 (기본값 - 최근 시세 백테스트로 잘 맞은 기법 우대)"],
            ["entry_exit_pref", "진입+청산 가산점 반영 (보유 중 놓친 이익까지 추적해 다음 진입 때 익절 목표 확대)"],
          ],
          hint: "이 시장의 매매 결정에 과거 데이터를 얼마나 반영할지 고릅니다. entry_exit_pref 는 종목별 청산 효율 기록이 최소 3건 쌓여야 실제로 반영되기 시작합니다." },
        { path: "swing.max_hold_days", label: "최대 보유 일수", kind: "num", min: 1, max: 120, step: 1, suffix: "일",
          hint: "이 기간을 넘기면 조건과 무관하게 정리합니다(시간 손절)." },
        { path: "swing.consecutive_loss_halt", label: "연속 손절 중단 횟수", kind: "num", min: 1, max: 10, step: 1,
          hint: "연속으로 이 횟수만큼 손절하면 신규 진입을 멈춥니다(보유 중인 종목의 청산은 계속됩니다)." },
        { path: "swing.loss_halt_cooldown_hours", label: "연속 손절 후 재개 간격(시간)", kind: "num", min: 1, max: 168, step: 1,
          hint: "★ 연속 손절로 멈춘 뒤 이 시간이 지나면 신규 매수를 자동으로 재개합니다(마지막 손절 시각 기준). 3시간이 기본이며, 다른 시장과 같은 시간 단위로 잽니다." },
        { path: "swing.poll_seconds", label: "보유 관리 주기(초)", kind: "num", min: 60, max: 7200, step: 60,
          hint: "일봉 기반이라 국내 단타처럼 자주 볼 필요가 없습니다 - 이 주기마다 보유 종목의 손절·추적손절만 확인합니다. 신규 진입 판정은 매일 정규장이 끝난 뒤 한 번만 합니다." },
      ],
    },
    {
      title: "실거래 안전장치", group: "시스템",
      fields: [
        { path: "live.require_preflight", label: "사전 점검 필수", kind: "bool",
          hint: "실거래 시작 전 계좌 상태를 반드시 먼저 확인합니다. 끄지 않는 것을 강력히 권합니다." },
        { path: "live.reconcile_every_loops", label: "계좌 대조 주기(루프 수)", kind: "num", min: 2, max: 120, step: 1,
          hint: "내부 상태와 실제 계좌를 얼마나 자주 맞춰볼지입니다. 계좌가 진실입니다." },
        { path: "live.degrade_after_failures", label: "저하 모드 진입 기준(연속 실패)", kind: "num", min: 1, max: 20, step: 1,
          hint: "시세를 못 보는 상태에서 사는 것은 눈 감고 사는 것입니다." },
        { path: "live.degrade_halt_minutes", label: "저하 모드 지속 시 완전 정지 시간", kind: "num", min: 1, max: 60, step: 1, suffix: "분",
          hint: "저하 모드가 이 시간 넘게 이어지면 매매를 완전히 멈춥니다." },
        { path: "live.adopt_unknown_holdings", label: "미확인 보유 종목 입양", kind: "bool",
          hint: "사용자가 직접 산 종목을 프로그램이 관리 대상으로 편입할지입니다. 기본은 건드리지 않는 쪽입니다." },
        { path: "live.cancel_orphans_on_start", label: "시작 시 미체결 고아 주문 정리", kind: "bool",
          hint: "크래시 당시 남은, 우리가 낸 미체결 주문만 정리합니다. 남의 주문은 절대 건드리지 않습니다." },
      ],
    },
    {
      title: "화면", group: "시스템",
      fields: [
        { path: "ui.session_idle_minutes", label: "자리 비움 잠금(분)", kind: "num", min: 0, max: 1440, step: 5,
          hint: "화면을 아무도 조작하지 않은 채 이 시간이 지나면 비밀번호를 다시 입력해야 합니다(0 = 사용 안 함). 자동 갱신은 조작으로 치지 않습니다." },
        { path: "ui.stream", label: "실시간 갱신", kind: "bool",
          hint: "꺼두면 화면이 자동으로 갱신되지 않습니다." },
        { path: "ui.tick_ms", label: "갱신 주기", kind: "num", min: 200, max: 10000, step: 100, suffix: "ms",
          hint: "너무 짧으면 브라우저가 버벅일 수 있습니다." },
        { path: "ui.chart_window", label: "차트 기본 구간", kind: "select",
          options: [["5m", "5분"], ["30m", "30분"], ["all", "전체"]],
          hint: "차트를 열었을 때 처음 보여줄 시간 범위입니다." },
        { path: "ui.animate", label: "화면 전환 애니메이션", kind: "bool",
          hint: "느린 기기에서는 꺼두면 더 매끄럽습니다." },
        { path: "ui.grid_density", label: "표 밀도", kind: "select",
          options: [["comfortable", "여유롭게"], ["compact", "빽빽하게"]],
          hint: "한 화면에 더 많은 행을 보고 싶으면 '빽빽하게'를 선택하세요." },
      ],
    },
    {
      title: "시뮬레이션", group: "시스템",
      note: "모드가 '가짜 데이터'일 때만 쓰입니다.",
      fields: [
        { path: "simulation.scenario", label: "시나리오", kind: "select",
          options: [
            ["normal", "평범한 날"],
            ["strong_theme", "강한 테마"],
            ["choppy", "방향 없는 날"],
            ["crash", "급락장"],
          ],
          hint: "손절·익절 로직을 극단 상황에서 검증하려면 crash 를 골라보세요." },
        { path: "simulation.speed", label: "재생 배속", kind: "num", min: 1, max: 3600, step: 1,
          hint: "1이면 실제 속도, 클수록 하루를 빨리 재생합니다." },
        { path: "simulation.seed", label: "난수 시드", kind: "num", min: 0, max: 9999, step: 1,
          hint: "같은 시드면 같은 하루가 재현됩니다." },
        { path: "simulation.start_time", label: "시작 시각", kind: "time",
          hint: "가짜 장이 몇 시부터 시작할지입니다." },
        { path: "simulation.history_days", label: "준비할 과거 일수", kind: "num", min: 1, max: 30, step: 1,
          hint: "시뮬레이션 전에 미리 만들어 둘 과거 데이터 기간입니다." },
        { path: "simulation.days", label: "시뮬레이션 총 일수", kind: "num", min: 1, max: 60, step: 1,
          hint: "여러 날을 이어 돌려야 성과 곡선이 의미를 갖습니다." },
      ],
    },
  ];

  // ━━ 필드 렌더링 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  // ★★★ "숫자에서 세 자리마다 ,를 찍도록" - 투자금액 같은 큰 금액은 원생
  // number 입력으로 보면 자릿수를 한눈에 세기 어렵다(10000000이 천만인지
  // 얼른 안 읽힌다). type=text + 쉼표 서식으로 바꾸고, 저장 시(collectConfig)
  // 에는 쉼표를 지워 순수 숫자로 되돌린다. app.js 의 won()과 같은 "ko-KR"
  // 로케일을 써서 표시 형식을 통일한다.
  function _thousandsFormat(v) {
    if (v == null || v === "") return "";
    const n = Number(v);
    return isNaN(n) ? "" : n.toLocaleString("ko-KR");
  }

  function _thousandsParse(s) {
    const digits = String(s == null ? "" : s).replace(/[^\d]/g, "");
    return digits === "" ? null : Number(digits);
  }

  function _onThousandsInput(ev) {
    const el = ev.target;
    // ★ 커서 앞의 "숫자 개수"를 세어 두고, 쉼표를 새로 입힌 뒤 같은 개수만큼
    // 지난 자리로 커서를 되돌린다 - 그냥 끝에 두면 중간에 숫자를 넣을 때마다
    // 커서가 맨 뒤로 튀는 흔한 버그가 생긴다.
    const pos = el.selectionStart == null ? el.value.length : el.selectionStart;
    const digitsBefore = el.value.slice(0, pos).replace(/[^\d]/g, "").length;
    const raw = el.value.replace(/[^\d]/g, "");
    el.value = raw === "" ? "" : Number(raw).toLocaleString("ko-KR");

    let seen = 0, newPos = el.value.length;
    for (let i = 0; i < el.value.length; i++) {
      if (/\d/.test(el.value[i])) seen++;
      if (seen === digitsBefore) { newPos = i + 1; break; }
    }
    if (digitsBefore === 0) newPos = 0;
    el.setSelectionRange(newPos, newPos);
  }

  function _buildThousandsInput(value) {
    const input = document.createElement("input");
    input.type = "text";
    input.inputMode = "numeric";
    input.autocomplete = "off";
    input.value = _thousandsFormat(value);
    input.addEventListener("input", _onThousandsInput);
    return input;
  }

  function buildField(f, raw, techs) {
    const wrap = document.createElement("div");
    wrap.className = "field";

    // ★★★ "설명 텍스트를 아이콘이나 제목을 마우스 오버했을 때만 나타나도록
    // 정리해서 깔끔한 UI 로, 스크롤을 최소화" - 설정 화면은 필드마다 긴
    // 힌트가 한 줄씩 깔려(104개) 스크롤이 가장 길었다. 힌트를 라벨 옆
    // 아이콘으로 옮기면 화면 높이가 절반 가까이 줄어든다.
    const label = document.createElement("label");
    label.textContent = f.label;
    if (f.hint) {
      // ★ 라벨 자체에도 title 을 달아 둔다 - 아이콘을 정확히 겨냥하지
      //   않고 라벨에만 올려도 설명이 보인다.
      label.title = f.hint;
      label.classList.add("has-help");
      label.appendChild(_fieldHelp(f.hint));
    }
    wrap.appendChild(label);

    const value = getPath(raw, f.path);
    let input;

    if (f.kind === "bool") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!value;
    } else if (f.kind === "select") {
      input = document.createElement("select");
      (f.options || []).forEach(([v, text]) => {
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = text;
        if (v === value) opt.selected = true;
        input.appendChild(opt);
      });
    } else if (f.kind === "num" && (f.max == null || f.max > 999)) {
      // ★ 상한이 없거나 999를 넘는 num 필드는 큰 값이 될 수 있어 won 과
      // 똑같이 쉼표 서식을 입힌다. 상한이 999 이하인 작은 개수 필드(동시
      // 보유 수 등)는 화살표로 조절하는 게 더 편해 기존 number 입력을 쓴다.
      input = _buildThousandsInput(value);
    } else if (f.kind === "num") {
      input = document.createElement("input");
      input.type = "number";
      if (f.min != null) input.min = f.min;
      if (f.max != null) input.max = f.max;
      if (f.step != null) input.step = f.step;
      input.value = value != null ? value : "";
    } else if (f.kind === "pct") {
      input = document.createElement("input");
      input.type = "number";
      input.step = f.step != null ? f.step : 0.1;
      if (f.min != null) input.min = f.min;
      if (f.max != null) input.max = f.max;
      // ★ 0.025 를 2.5 로 보여준다 - 사용자는 퍼센트로 생각한다.
      input.value = value != null ? Number((value * 100).toFixed(6)) : "";
    } else if (f.kind === "won") {
      input = _buildThousandsInput(value);
    } else if (f.kind === "time") {
      input = document.createElement("input");
      input.type = "time";
      input.value = value || "";
    } else if (f.kind === "techlist") {
      input = document.createElement("div");
      input.className = "techlist";
      const list = (techs && techs[f.which]) || [];
      const checkedSet = new Set(value || []);
      list.forEach((t) => {
        const row = document.createElement("label");
        row.className = "tech-row";
        row.style.display = "block";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = t.key;
        cb.checked = checkedSet.has(t.key);
        // ★ which=="exit" 이고 key=="fixed" 면 checked+disabled. 손절 없는 설정은 못 만든다.
        if (f.which === "exit" && t.key === "fixed") {
          cb.checked = true;
          cb.disabled = true;
        }
        row.appendChild(cb);
        const text = document.createElement("span");
        text.innerHTML = ` <b>${escAttr(t.label)}</b> - ${escAttr(t.description || "")}`;
        row.appendChild(text);
        input.appendChild(row);
      });
    } else if (f.kind === "checklist") {
      input = document.createElement("div");
      input.className = "checklist";
      const checkedSet = new Set(value || []);
      (f.options || []).forEach(([v, text]) => {
        const row = document.createElement("label");
        row.style.display = "block";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = v;
        cb.checked = checkedSet.has(v);
        row.appendChild(cb);
        row.appendChild(document.createTextNode(" " + text));
        input.appendChild(row);
      });
    } else if (f.kind === "text") {
      input = document.createElement("input");
      input.type = "text";
      input.autocomplete = "off";
      input.value = value || "";
      if (f.placeholder) input.placeholder = f.placeholder;
    } else if (f.kind === "taglist") {
      // ★ 쉼표로 구분한 종목코드 목록(예: "AAPL, NVDA") - 저장 시 배열로 바꾼다.
      // (searchlist 로 다 옮겨가면서 지금은 쓰는 필드가 없지만, 다른 용도로 다시
      // 쓸 수 있어 남겨 둔다.)
      input = document.createElement("input");
      input.type = "text";
      input.autocomplete = "off";
      input.value = Array.isArray(value) ? value.join(", ") : (value || "");
      if (f.placeholder) input.placeholder = f.placeholder;
    } else if (f.kind === "searchlist") {
      // ★★★ "코드를 직접 치는 게 아니라 이름으로 검색해서 고르게 해달라"(관심종목
      // 6개 필드 공통 요청) - 그리고 "검색 결과든 이미 추가된 목록이든 코드만 보이지
      // 말고 항상 '종목명(코드)'로 보여라"(두 번 강조). 저장되는 값(codes 배열)은
      // 예전 taglist 와 완전히 같은 코드 문자열 배열이다 - collectConfig 는 techlist/
      // checklist 와 똑같이 숨은 체크박스를 읽어서 배열을 만든다(아래 hidden 참고).
      input = document.createElement("div");
      input.className = "searchlist";

      const codes = Array.isArray(value) ? value.slice() : [];
      const nameCache = Object.create(null); // code -> 이름(모르면 코드 자신을 씀)
      const endpoint = {
        crypto: "/api/crypto/markets/search",
        domestic: "/api/domestic/stocks/search",
        overseas: "/api/overseas/stocks/search",
      }[f.market] || null;

      // ★ collectConfig 가 읽는 자리 - 실제로 눈에 보이는 건 아래 chips 다.
      const hidden = document.createElement("div");
      hidden.style.display = "none";
      input.appendChild(hidden);

      function syncHidden() {
        hidden.innerHTML = "";
        codes.forEach((c) => {
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = true;
          cb.value = c;
          hidden.appendChild(cb);
        });
      }

      const chips = document.createElement("div");
      chips.className = "searchlist-chips";

      function renderChips() {
        chips.innerHTML = "";
        codes.forEach((c) => {
          const chip = document.createElement("span");
          chip.className = "badge tech searchlist-chip";
          chip.textContent = `${nameCache[c] || c}(${c})`;
          const x = document.createElement("button");
          x.type = "button";
          x.className = "searchlist-remove";
          x.textContent = "×";
          x.title = "제거";
          x.addEventListener("click", () => {
            const i = codes.indexOf(c);
            if (i >= 0) codes.splice(i, 1);
            syncHidden();
            renderChips();
          });
          chip.appendChild(x);
          chips.appendChild(chip);
        });
      }

      const searchWrap = document.createElement("div");
      searchWrap.className = "searchlist-searchwrap";
      const searchInput = document.createElement("input");
      searchInput.type = "text";
      searchInput.autocomplete = "off";
      searchInput.placeholder = f.placeholder || "이름 또는 코드로 검색";
      const results = document.createElement("div");
      results.className = "searchlist-results";
      results.style.display = "none";
      searchWrap.appendChild(searchInput);
      searchWrap.appendChild(results);

      function showResults(rows) {
        results.innerHTML = "";
        if (!rows || !rows.length) {
          results.style.display = "none";
          return;
        }
        rows.forEach((r) => {
          const item = document.createElement("div");
          item.className = "searchlist-result";
          item.textContent = `${r.name}(${r.code})`;
          item.addEventListener("click", () => {
            nameCache[r.code] = r.name;
            if (!codes.includes(r.code)) codes.push(r.code);
            syncHidden();
            renderChips();
            searchInput.value = "";
            results.style.display = "none";
            searchInput.focus();
          });
          results.appendChild(item);
        });
        results.style.display = "block";
      }

      // ★ 250ms 디바운스 - 한 글자 칠 때마다 서버를 때리지 않는다.
      let debounceTimer = null;
      searchInput.addEventListener("input", () => {
        clearTimeout(debounceTimer);
        const q = searchInput.value.trim();
        if (!q || !endpoint) {
          results.style.display = "none";
          return;
        }
        debounceTimer = setTimeout(() => {
          fetch(`${endpoint}?q=${encodeURIComponent(q)}`)
            .then((r) => r.json())
            .then((data) => showResults(data && data.rows))
            .catch(() => showResults([]));
        }, 250);
      });
      searchInput.addEventListener("blur", () => {
        // ★ 클릭이 결과 항목에 먼저 닿을 시간을 준다(즉시 닫으면 클릭이 씹힌다).
        setTimeout(() => { results.style.display = "none"; }, 150);
      });

      // ★ 이미 저장된 코드는 이름을 모른다(코드만 있음) - 정확 일치 검색으로
      // 하나씩 물어봐 nameCache 를 채운다. 실패해도 코드 자체를 이름 자리에 쓴다.
      if (endpoint) {
        codes.forEach((c) => {
          fetch(`${endpoint}?q=${encodeURIComponent(c)}`)
            .then((r) => r.json())
            .then((data) => {
              const hit = (data && data.rows || []).find((r) => r.code === c);
              if (hit) {
                nameCache[c] = hit.name;
                renderChips();
              }
            })
            .catch(() => {});
        });
      }

      syncHidden();
      renderChips();
      input.appendChild(searchWrap);
      input.appendChild(chips);
    } else if (f.kind === "secret") {
      input = document.createElement("input");
      input.type = "password";
      input.autocomplete = "off";
      input.value = value || "";
      if (f.placeholder) input.placeholder = f.placeholder;
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.value = value != null ? value : "";
    }

    input.dataset.path = f.path;
    input.dataset.kind = f.kind;
    wrap.appendChild(input);

    if (f.suffix) {
      const suf = document.createElement("span");
      suf.className = "suffix";
      suf.textContent = f.suffix;
      wrap.appendChild(suf);
    }

    // ★ 힌트는 위 라벨의 아이콘으로 옮겼다 - 본문에 또 깔지 않는다.
    return wrap;
  }

  // ━━ 폼 -> 설정 수집 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  function collectConfig(root, raw) {
    // ★ 원본 raw 를 바탕으로 만들어야 스키마에 없는 키가 지워지지 않는다.
    const out = JSON.parse(JSON.stringify(raw));
    const inputs = root.querySelectorAll("[data-path]");

    inputs.forEach((el) => {
      const path = el.dataset.path;
      const kind = el.dataset.kind;
      let value;

      if (kind === "bool") {
        value = el.checked;
      } else if (kind === "techlist" || kind === "checklist" || kind === "searchlist") {
        // ★ searchlist 도 techlist/checklist 와 같은 방식(숨은 체크박스)으로 읽는다 -
        // 실제 값은 검색해서 고른 코드 배열이다(위 buildField 의 searchlist 참고).
        value = Array.from(el.querySelectorAll("input[type=checkbox]"))
          .filter((cb) => cb.checked)
          .map((cb) => cb.value);
      } else if (kind === "select" || kind === "time") {
        value = el.value;
      } else if (kind === "taglist") {
        value = el.value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
      } else if (kind === "text" || kind === "secret") {
        value = el.value.trim();
      } else {
        if (el.value === "") {
          value = null;
        } else {
          // ★ won 과 상한이 큰 num 은 쉼표 서식 text 입력이다(el.type==="text") -
          // 쉼표를 지우고 숫자로 되돌린다. 소수를 쓰는 num(예: 배수 0.6)은
          // 여전히 number 입력이라 parseFloat 로 그대로 읽는다.
          const n = (kind === "num" || kind === "won") && el.type === "text"
            ? _thousandsParse(el.value)
            : parseFloat(el.value);
          value = kind === "pct" ? n / 100 : n;
        }
      }
      setPath(out, path, value);
    });

    return out;
  }

  // ━━ 기법 배지 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  let _techCache = null;
  let _techPromise = null;

  function loadTechniques() {
    if (_techCache) return Promise.resolve(_techCache);
    if (_techPromise) return _techPromise;
    _techPromise = fetch("/api/techniques")
      .then((r) => r.json())
      .then((data) => {
        _techCache = data;
        return data;
      })
      .catch(() => {
        _techCache = { entry: [], exit: [] };
        return _techCache;
      });
    return _techPromise;
  }

  function techBadge(key, opts) {
    opts = opts || {};
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "badge tech";
    btn.dataset.tech = key;
    btn.textContent = opts.label || key;
    return btn;
  }

  function techBadgeHTML(key, opts) {
    opts = opts || {};
    // ★★ 표 안에서는 반드시 이것을 쓴다 (A-24) - DataGrid 결과는 innerHTML 로
    // 들어가 onclick 이 사라지므로, data-tech 속성만 남긴 순수 문자열을 낸다.
    return `<button type="button" class="badge tech" data-tech="${escAttr(key)}">${escAttr(opts.label || key)}</button>`;
  }

  let _openPopup = null;

  function _closePopup() {
    if (_openPopup) {
      _openPopup.remove();
      _openPopup = null;
    }
  }

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button.badge.tech[data-tech]");
    const wasOpenForThis = _openPopup && _openPopup.dataset.forKey === (btn && btn.dataset.tech);

    _closePopup();
    if (!btn || wasOpenForThis) return; // 같은 배지를 다시 누르면 닫기만 한다.

    const techs = await loadTechniques();
    const all = (techs.entry || []).concat(techs.exit || []);
    const t = all.find((x) => x.key === btn.dataset.tech);
    if (!t) return;

    const popup = document.createElement("div");
    popup.className = "tech-popup card";
    popup.dataset.forKey = btn.dataset.tech;
    popup.style.position = "absolute";
    popup.style.zIndex = "50";
    popup.style.maxWidth = "320px";
    popup.style.fontSize = "13px";

    const rect = btn.getBoundingClientRect();
    popup.style.left = rect.left + window.scrollX + "px";
    popup.style.top = rect.bottom + window.scrollY + 4 + "px";

    // ★ 원전을 보여주는 것이 요점이다 - 지어낸 규칙과 문헌에 있는 기법을 구분한다.
    popup.innerHTML =
      `<div style="font-weight:600;">${escAttr(t.label)} ` +
      `<span style="color:var(--muted);font-weight:400;">(${t.phase === "entry" ? "진입" : "청산"})</span></div>` +
      `<div style="margin-top:4px;">${escAttr(t.description || "")}</div>` +
      `<div style="margin-top:8px;font-weight:600;">원전</div>` +
      `<div>${escAttr(t.origin || "")}</div>` +
      `<div style="margin-top:8px;font-weight:600;">표준값과 우리 설정</div>` +
      `<div>${escAttr(t.standard || "")}</div>`;

    document.body.appendChild(popup);
    _openPopup = popup;
    ev.stopPropagation();
  });

  window.UI = Object.assign(window.UI || {}, {
    CONFIG_SCHEMA,
    fieldTier,
    buildField,
    collectConfig,
    getPath,
    setPath,
    techBadge,
    techBadgeHTML,
    loadTechniques,
  });
})();
