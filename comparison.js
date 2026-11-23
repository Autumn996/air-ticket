(() => {
  const get = (id) => document.getElementById(id);
  const data = JSON.parse(get("comparison-data").textContent);
  const filters = get("filters");
  const compactLayout = matchMedia("(max-width: 850px)");
  const filterToggle = get("filter-toggle");
  const resetFilters = get("reset-filters");
  const costForm = get("cost-form");
  const costCity = get("expense-city");
  const currency = new Intl.NumberFormat("zh-CN", {
    style: "currency", currency: "CNY", minimumFractionDigits: 0, maximumFractionDigits: 2,
  });
  const money = (cents) => currency.format(cents / 100);
  const fieldNames = ["lodging", "transport", "other"];
  const storageKey = "shanghai-holiday-fare-comparison";
  let state = {};
  try {
    state = JSON.parse(localStorage.getItem(storageKey) || "{}") || {};
  } catch {
    state = {};
  }
  const costs = state.costs ||= {};
  const tripKey = data.outbound + "~" + data.returning;
  const tripCosts = costs[tripKey] ||= {};
  let adults = Math.max(1, Math.min(9, Number(state.adults) || 1));
  let mobileFiltersOpen = false;
  const selectedAdults = new URLSearchParams(location.search).get("adults");
  if (/^[1-9]$/.test(selectedAdults ?? "")) {
    adults = Number(selectedAdults);
    state.adults = adults;
    persist();
    const url = new URL(location.href);
    url.searchParams.delete("adults");
    history.replaceState(null, "", url);
  }

  for (const control of filters.elements) {
    if (!control.name || state.filters?.[control.name] == null) continue;
    if (control.type === "checkbox") control.checked = Boolean(state.filters[control.name]);
    else control.value = state.filters[control.name];
  }
  const advanced = readFilters();
  get("advanced").open = Boolean(advanced.earliest || advanced.latest || advanced.stay || advanced.stale);
  get("adults").value = adults;
  costCity.replaceChildren(...data.cities.map((city) => new Option(city.name, city.id)));
  costForm.querySelector("button").disabled = !data.cities.length;

  function persist() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(state));
      return true;
    } catch {
      return false;
    }
  }

  function amount(value) {
    return value === "" || !Number.isFinite(Number(value)) || Number(value) < 0 ? null : Number(value);
  }

  function readFilters() {
    const result = {};
    for (const control of filters.elements) {
      if (control.name) result[control.name] = control.type === "checkbox" ? control.checked : control.value;
    }
    return result;
  }

  function syncFilterPanel() {
    const compact = compactLayout.matches;
    filters.hidden = compact && !mobileFiltersOpen;
    resetFilters.hidden = filters.hidden;
    filterToggle.hidden = !compact;
    filterToggle.textContent = mobileFiltersOpen ? "收起" : "展开";
    filterToggle.setAttribute("aria-expanded", String(compact && mobileFiltersOpen));
    const preferences = readFilters();
    const active = ["budget", "airport", "sameDay", "avoidRedEye", "earliest", "latest", "stay", "stale"]
      .filter((name) => preferences[name]).length;
    get("filter-state").hidden = !filters.hidden || !active;
    get("filter-state").textContent = "已启用 " + active + " 项";
  }

  function fillCosts() {
    const entry = tripCosts[costCity.value] || {};
    for (const field of fieldNames) get(field).value = entry[field] == null ? "" : entry[field] / 100;
  }

  function estimatedTotal(city, option) {
    const entry = tripCosts[city.id];
    if (!entry || entry.lodging == null || entry.transport == null) return null;
    return partyCents(option) + entry.lodging + entry.transport + (entry.other ?? 0);
  }

  function partyCents(option) {
    return option.partyQuotes[adults]?.cents ?? option.cents * adults;
  }

  const arrivesOvernight = (leg) => leg.arrival.time.slice(0, 10) !== leg.departure.time.slice(0, 10);
  const airportName = (airport) => {
    if (airport.code === "SHA") return "上海虹桥";
    if (airport.code === "PVG") return "上海浦东";
    return airport.name.replace(/(?:国际)?机场$/, "");
  };

  function routeLine(leg) {
    const line = get("route-line").content.firstElementChild.cloneNode(true);
    const field = (name) => line.querySelector('[data-leg="' + name + '"]');
    field("dep-time").textContent = leg.departure.time.slice(11);
    field("dep-airport").textContent = airportName(leg.departure);
    field("arr-time").textContent = leg.arrival.time.slice(11);
    field("arr-airport").textContent = airportName(leg.arrival);
    field("next-day").hidden = !arrivesOvernight(leg);
    field("number").textContent = leg.number;
    return line;
  }

  function stayLabel(minutes) {
    const days = Math.floor(minutes / 1440);
    const hours = Math.floor(minutes / 60) % 24;
    return (days ? days + " 天 " : "") + hours + " 小时";
  }

  function eligible(option, preferences) {
    const budget = amount(preferences.budget);
    const minimumStay = amount(preferences.stay);
    return (budget == null || option.cents <= Math.round(budget * 100))
      && (!preferences.airport || option.out.departure.code === preferences.airport)
      && (!preferences.sameDay || !arrivesOvernight(option.out))
      && (!preferences.avoidRedEye || !option.redEye)
      && (!preferences.earliest || option.out.departure.time.slice(11) >= preferences.earliest)
      && (!preferences.latest || option.back.arrival.time <= data.returning + " " + preferences.latest)
      && (minimumStay == null || option.stay >= minimumStay * 60);
  }

  function ordering(a, b, sort) {
    if (sort === "stay" && a.option.stay !== b.option.stay) return b.option.stay - a.option.stay;
    if (sort === "total" && a.total !== b.total) {
      if (a.total == null) return 1;
      if (b.total == null) return -1;
      return a.total - b.total;
    }
    return a.option.cents - b.option.cents || b.option.stay - a.option.stay;
  }

  function cityCard({city, option, total}, preferences) {
    const card = get("city-card").content.firstElementChild.cloneNode(true);
    const bind = (name) => card.querySelector('[data-value="' + name + '"]');
    bind("city").textContent = city.name;
    bind("code").textContent = option.out.arrival.code;
    bind("price").textContent = money(option.cents);
    const tags = [];
    if (city.stale) tags.push("此前报价");
    if (option.out.departure.code !== option.back.arrival.code) tags.push("出发与抵沪机场不同");
    if (option.median != null && option.cents * 100 <= option.median * 85) {
      tags.push("低于同组合近期中位价 " + Math.round((1 - option.cents / option.median) * 100) + "%");
    }
    for (const text of tags) {
      const tag = document.createElement("span");
      tag.className = text.includes("机场不同") ? "tag warning-tag" : "tag";
      tag.textContent = text;
      bind("badges").append(tag);
    }
    bind("out").append(routeLine(option.out));
    bind("back").append(routeLine(option.back));
    bind("stay").textContent = "落地至返航约 " + stayLabel(option.stay);
    const checked = option.partyQuotes[adults];
    const partyPrice = money(partyCents(option));
    const priceLabel = checked ? "机票已核价 " : "机票估算 ";
    bind("total").textContent = total == null
      ? adults + " 人" + priceLabel + partyPrice + " · 住宿或交通待填"
      : adults + " 人整趟约 " + money(total) + "，" + priceLabel + partyPrice;
    bind("total").hidden = adults === 1 && total == null && !checked && !tripCosts[city.id];
    bind("source").textContent = city.updated.slice(5) + " 采集";
    if (checked) bind("source").textContent += " · " + checked.checked + " 核对全员价";
    const detail = new URL(city.detail);
    if (preferences.avoidRedEye) detail.searchParams.set("avoid_red_eye", "1");
    bind("detail").href = detail.href;
    const party = new URL(option.party);
    party.searchParams.set("adults", adults);
    bind("party").href = party.href;
    bind("watch").href = option.watch;
    bind("booking").href = city.booking;
    return card;
  }

  function pendingItem(city, reason) {
    const row = document.createElement("li");
    const label = document.createElement("span");
    const link = document.createElement("a");
    label.textContent = city.name + " · " + reason + " · " + city.refDay + " 参考价 " + money(city.ref);
    link.textContent = "航班详情";
    link.href = city.detail;
    row.append(label, link);
    return row;
  }

  function render() {
    const preferences = readFilters();
    const picks = [];
    const pending = [];
    let saved = 0;
    let todayCovered = 0;
    for (const city of data.cities) {
      if (!city.options.length) {
        pending.push(pendingItem(city, city.attempted ? "今天查过，暂无可核对组合" : "待查航班"));
        continue;
      }
      saved++;
      if (!city.stale) todayCovered++;
      if (city.stale && !preferences.stale) {
        pending.push(pendingItem(city, "有此前的组合报价"));
        continue;
      }
      const choices = city.options.filter((option) => eligible(option, preferences))
        .map((option) => ({city, option, total: estimatedTotal(city, option)}));
      if (choices.length) {
        choices.sort((a, b) => ordering(a, b, preferences.sort));
        picks.push(choices[0]);
      } else {
        pending.push(pendingItem(city, "已查方案与条件不符"));
      }
    }
    picks.sort((a, b) => ordering(a, b, preferences.sort));
    get("cards").replaceChildren(...picks.map((pick) => cityCard(pick, preferences)));
    get("metric-cities").textContent = data.cities.length;
    get("metric-saved").textContent = saved;
    get("metric-today").textContent = todayCovered;
    get("metric-matching").textContent = picks.length;
    get("coverage").textContent = data.cities.length + " 城有参考价 · " + saved + " 城已存往返明细"
      + " · 今天核对 " + todayCovered + " 城 · " + picks.length + " 城符合当前筛选";
    get("incomplete-totals").hidden = preferences.sort !== "total" || picks.every((pick) => pick.total != null);
    get("empty").hidden = picks.length > 0;
    get("pending").hidden = pending.length === 0;
    get("pending-label").textContent = "查看待查或暂未匹配的 " + pending.length + " 个目的地";
    get("pending-rows").replaceChildren(...pending);
  }

  filters.addEventListener("submit", (event) => event.preventDefault());
  filters.addEventListener("input", () => {
    state.filters = readFilters();
    persist();
    syncFilterPanel();
    render();
  });
  resetFilters.addEventListener("click", () => {
    filters.reset();
    state.filters = readFilters();
    persist();
    get("advanced").open = false;
    syncFilterPanel();
    render();
  });
  filterToggle.addEventListener("click", () => {
    mobileFiltersOpen = !mobileFiltersOpen;
    syncFilterPanel();
  });
  compactLayout.addEventListener("change", syncFilterPanel);
  costCity.addEventListener("change", () => {
    fillCosts();
    get("cost-status").textContent = "";
  });
  costForm.addEventListener("submit", (event) => {
    event.preventDefault();
    adults = Number(get("adults").value);
    state.adults = adults;
    tripCosts[costCity.value] = Object.fromEntries(fieldNames.map((field) => {
      const value = amount(get(field).value);
      return [field, value == null ? null : Math.round(value * 100)];
    }));
    get("cost-status").textContent = persist() ? "已保存在本浏览器。" : "已应用于当前页面。";
    render();
  });
  get("batch").addEventListener("submit", (event) => {
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    button.textContent = "正在核对往返航班…";
  });

  fillCosts();
  syncFilterPanel();
  render();
})();
