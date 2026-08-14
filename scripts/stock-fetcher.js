#!/usr/bin/env node
/**
 * 多市场行情数据采集工具
 * 数据源：腾讯财经（主，支持 A股/港股/美股/ETF）+ 新浪财经（A股备用）
 * 免费、无需注册
 *
 * 支持代码格式：
 *   A股:   600030 / sh600030 / sz000001 / bj430047
 *   港股:  hk00700 / 00700（自动识别为港股）
 *   美股:  usAAPL / AAPL（自动识别为美股）
 *   ETF:   sh510300 / 510300 / hk02800（港股ETF） / usSPY（美股ETF）
 */

const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");

const WATCHLIST_FILE = path.join(__dirname, "..", "runtime", "data", "watchlist.json");

// ─── HTTP 工具 ──────────────────────────────────────────

function fetchRaw(url, options = {}) {
  return new Promise((resolve, reject) => {
    const headers = {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      ...(options.headers || {}),
    };
    const args = [
      "-L", "--silent", "--show-error", "--max-time", "15", "--connect-timeout", "8",
      "--retry", "2", "--retry-delay", "1", "--retry-all-errors",
    ];
    for (const [k, v] of Object.entries(headers)) args.push("-H", `${k}: ${v}`);
    args.push(url);
    execFile("curl", args, { encoding: "buffer", maxBuffer: 20 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) return reject(new Error(stderr?.toString()?.trim() || err.message || "请求失败"));
      resolve(stdout);
    });
  });
}

function fetchText(url, options = {}) {
  const enc = options.encoding || "utf8";
  delete options.encoding;
  return fetchRaw(url, options).then((buf) => new TextDecoder(enc).decode(buf));
}

// ─── 代码解析 ──────────────────────────────────────────

// 识别市场类型
function detectMarket(input) {
  let code = input.trim();
  const lower = code.toLowerCase();
  if (lower.startsWith("hk")) return { market: "hk", code: code.slice(2).toUpperCase() };
  if (lower.startsWith("us")) return { market: "us", code: code.slice(2).toUpperCase() };
  if (/^(sh|sz|bj)\d{6}$/.test(lower)) return { market: "cn", code: lower };
  if (/^\d{5}$/.test(code)) {
    return { market: "hk", code };
  }
  if (/^\d{6}$/.test(code)) {
    const first = code[0];
    // A股: 0/3→深市, 6→沪市, 4/8→北交所
    // ETF/基金: 5→沪市(5xxxxx), 1→深市(1xxxxx)
    if (["6", "5"].includes(first)) return { market: "cn", code: `sh${code}` };
    if (["4", "8"].includes(first)) return { market: "cn", code: `bj${code}` };
    return { market: "cn", code: `sz${code}` };
  }
  // 纯字母（无 us 前缀）→ 美股
  if (/^[A-Za-z.]+$/.test(code) && code.length <= 10) {
    return { market: "us", code: code.toUpperCase() };
  }
  return null;
}

// 转腾讯代码
function toTencentCode(market, code) {
  if (market === "cn") return code; // sh600030
  if (market === "hk") return `hk${code}`; // hk00700
  if (market === "us") return `us${code}`; // usAAPL
  return null;
}

// ─── 腾讯实时行情 ──────────────────────────────────────

/**
 * 腾讯接口 qt.gtimg.cn（统一格式，A股/港股/美股/ETF 一致）
 * f1=市场标记(1/100/200) f2=名称 f3=代码 f4=现价 f5=昨收 f6=今开 f7=成交量
 * f8=外盘 f9=内盘 f10-29=买卖盘口
 * f30=时间 f31=涨跌 f32=涨跌% f33=最高 f34=最低
 * f35=现价/量/额(A股)或币种(美股) f36=成交量(重复) f37=成交额(A股)或成交量(美股)
 * f38=换手率(A股)或成交额(港股) f39=PE f40=周期... f43=振幅 f44=流通市值 f45=总市值
 * 港股: f46=52周高 f47=52周低 f48=振幅
 * 美股: f48=52周高 f49=52周低
 */
async function getTencentRealtime(market, code) {
  const tencentCode = toTencentCode(market, code);
  const url = `https://qt.gtimg.cn/q=${tencentCode}`;
  const text = await fetchText(url, { encoding: "gbk" });

  const match = text.match(/="(.+)"/);
  if (!match || !match[1]) return { error: "未获取到数据，请检查代码" };
  const f = match[1].split("~");
  if (f.length < 5) return { error: "返回数据不完整" };

  const data = {
    market,
    code: tencentCode,
    name: f[1],
    price: parseFloat(f[3]),
    prev_close: parseFloat(f[4]),
    open: parseFloat(f[5]),
    volume: parseFloat(f[6]),
    time: f[30] || "",
  };

  // 涨跌
  data.change = parseFloat(f[31]) || 0;
  data.change_pct = parseFloat(f[32]) || 0;
  data.high = parseFloat(f[33]);
  data.low = parseFloat(f[34]);

  // 市场特有字段
  if (market === "cn") {
    data.amount = parseFloat(f[37]); // 成交额(万元)
    data.turnover = parseFloat(f[38]); // 换手率
    data.pe = parseFloat(f[39]); // PE
    data.amplitude = parseFloat(f[43]); // 振幅
    data.float_mv = parseFloat(f[44]); // 流通市值(亿)
    data.total_mv = parseFloat(f[45]); // 总市值(亿)
  } else if (market === "hk") {
    data.amount = parseFloat(f[38]); // 成交额
    data.turnover = parseFloat(f[39]); // 换手率
    data.pe = parseFloat(f[40]); // PE
    data.high_52w = parseFloat(f[46]);
    data.low_52w = parseFloat(f[47]);
    data.amplitude = parseFloat(f[48]);
  } else if (market === "us") {
    data.currency = f[35] || "USD";
    data.amount = parseFloat(f[37]); // 成交量(股)
    data.market_cap = parseFloat(f[45]);
    data.pe = parseFloat(f[39]);
    data.high_52w = parseFloat(f[48]);
    data.low_52w = parseFloat(f[49]);
  }

  data.change = isNaN(data.change) ? 0 : data.change;
  data.change_pct = isNaN(data.change_pct) ? 0 : data.change_pct;
  return data;
}

// ─── 腾讯K线 ──────────────────────────────────────────

/**
 * 腾讯K线接口 web.ifzq.gtimg.cn
 * param: <代码>,day,,,<数量>,qfq(前复权) 或 空(不复权)
 * 返回 data[code].day 或 .qfqday 数组: [日期, 开盘, 收盘, 最高, 最低, 成交量]
 */
async function getTencentHistory(market, code, period = "day", count = 1023, fq = "qfq") {
  let tencentCode = toTencentCode(market, code);
  // 美股K线统一用 .OQ 后缀（如 usAAPL.OQ）
  if (market === "us" && !tencentCode.includes(".")) tencentCode += ".OQ";
  const url = `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=${tencentCode},${period},,,${count},${fq}`;
  const text = await fetchText(url, { encoding: "utf8" });

  let json;
  try {
    json = JSON.parse(text);
  } catch {
    return { error: "K线数据解析失败" };
  }
  if (json.code !== 0 || !json.data) return { error: "暂无K线数据" };

  const key = Object.keys(json.data)[0];
  if (!key) return { error: "暂无K线数据" };
  const node = json.data[key];

  // 腾讯返回 day 或 qfqday 字段
  let rows = node[period] || node[`${period === "day" ? "qfq" : ""}day`] || node.qfqday || node.day;
  if (!Array.isArray(rows)) rows = [];

  const data = rows.map((r) => ({
    date: r[0],
    open: parseFloat(r[1]),
    close: parseFloat(r[2]),
    high: parseFloat(r[3]),
    low: parseFloat(r[4]),
    volume: parseFloat(r[5]),
  }));

  return { code: tencentCode, count: data.length, data };
}

// ─── 新浪实时行情（A股备用） ──────────────────────────

function parseSinaCode(input) {
  let code = input.trim().toLowerCase();
  if (/^(sh|sz|bj)\d{6}$/.test(code)) return code;
  if (/^\d{6}$/.test(code)) {
    const first = code[0];
    if (["6", "5"].includes(first)) return `sh${code}`;
    if (["4", "8"].includes(first)) return `bj${code}`;
    return `sz${code}`;
  }
  return null;
}

async function getSinaRealtime(sinaCode) {
  const url = `https://hq.sinajs.cn/list=${sinaCode}`;
  const text = await fetchText(url, {
    headers: { Referer: "https://finance.sina.com.cn" },
    encoding: "gbk",
  });
  const match = text.match(/"(.+)"/);
  if (!match) return { error: "未获取到数据，请检查股票代码" };
  const fields = match[1].split(",");
  if (fields.length < 32) return { error: "返回数据不完整" };
  return {
    market: "cn",
    code: sinaCode,
    name: fields[0],
    open: parseFloat(fields[1]),
    prev_close: parseFloat(fields[2]),
    price: parseFloat(fields[3]),
    high: parseFloat(fields[4]),
    low: parseFloat(fields[5]),
    volume: parseInt(fields[8]),
    amount: parseFloat(fields[9]),
    bid1: parseFloat(fields[11]),
    bid1_vol: parseInt(fields[12]),
    ask1: parseFloat(fields[21]),
    ask1_vol: parseInt(fields[22]),
    date: fields[30],
    time: fields[31],
    change: 0,
    change_pct: 0,
  };
}

async function getSinaHistory(sinaCode) {
  const url = `https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=${sinaCode}&scale=240&ma=no&datalen=1023`;
  const text = await fetchText(url, {
    headers: { Referer: "https://finance.sina.com.cn" },
  });
  let raw;
  try {
    raw = JSON.parse(text);
  } catch {
    return { error: "历史数据解析失败" };
  }
  if (!Array.isArray(raw) || raw.length === 0) return { error: "暂无历史数据" };
  const data = raw.map((d) => ({
    date: d.day,
    open: parseFloat(d.open),
    high: parseFloat(d.high),
    low: parseFloat(d.low),
    close: parseFloat(d.close),
    volume: parseInt(d.volume),
  }));
  return { code: sinaCode, count: data.length, data };
}

// ─── 搜索 ───────────────────────────────────────────────

/**
 * 腾讯搜索 smartbox.gtimg.cn
 * 返回格式: v_hint="sh~000847~腾讯济安~txja~ZS^hk~00700~腾讯控股~txkg~GP^..."
 * 类型: ZS=指数, GP=股票, ETF=ETF, QZ=权证/其他
 */
async function searchTencent(keyword) {
  const url = `https://smartbox.gtimg.cn/s3/?v=2&q=${encodeURIComponent(keyword)}&t=all`;
  const text = await fetchText(url, { encoding: "gbk" });
  const cleaned = text.replace(/^v_hint="/, "").replace(/"\s*$/, "");
  const items = cleaned.split("^").filter(Boolean);

  const results = [];
  for (const item of items) {
    const parts = item.split("~");
    if (parts.length >= 5) {
      const market = parts[0];
      const code = parts[1];
      const name = parts[2];
      const type = parts[4];
      let marketName = "";
      let symbol = code;
      if (market === "sh") { marketName = "沪市"; symbol = `sh${code}`; }
      else if (market === "sz") { marketName = "深市"; symbol = `sz${code}`; }
      else if (market === "bj") { marketName = "北交所"; symbol = `bj${code}`; }
      else if (market === "hk") { marketName = "港股"; symbol = `hk${code}`; }
      else if (market === "us") { marketName = "美股"; symbol = `us${code}`; }

      const typeName =
        type === "ZS" ? "指数" :
        type === "GP" ? "股票" :
        type === "ETF" ? "ETF" :
        type === "QZ" ? "权证/其他" : type;

      results.push({ code, name, market, marketName, symbol, type, typeName });
    }
  }
  return results;
}

// 新浪搜索（保留）
async function searchSina(keyword) {
  const url = `https://suggest3.sinajs.cn/suggest/type=11&key=${encodeURIComponent(keyword)}`;
  const text = await fetchText(url, {
    headers: { Referer: "https://finance.sina.com.cn" },
    encoding: "gbk",
  });
  const cleaned = text.replace(/^var suggestvalue="?/, "").replace(/"?\s*$/, "");
  const items = cleaned.split(";").filter(Boolean);
  const results = [];
  for (const item of items) {
    const parts = item.split(",");
    if (parts.length >= 4) {
      const symbol = parts[3];
      results.push({
        code: parts[2],
        name: parts[0],
        market: symbol.startsWith("sh") ? "沪市" : symbol.startsWith("sz") ? "深市" : "北交所",
        symbol,
      });
    }
  }
  return results;
}

async function searchStock(keyword) {
  try {
    return await searchTencent(keyword);
  } catch {
    try {
      return await searchSina(keyword);
    } catch (e) {
      return { error: `搜索失败: ${e.message}` };
    }
  }
}

// ─── 市场候选列表（选股第一阶段） ────────────────────────

function numeric(value) {
  if (value === null || value === undefined || value === "" || value === "--") return 0;
  const parsed = Number(String(value).replace(/[$,%，]/g, "").trim());
  return Number.isFinite(parsed) ? parsed : 0;
}

function normalizeSinaCandidate(row, market) {
  const price = numeric(row.trade ?? row.lasttrade);
  const volume = numeric(row.volume);
  const symbol = market === "hk"
    ? String(row.symbol || "").padStart(5, "0")
    : String(row.code || row.symbol || "").replace(/^(sh|sz|bj)/i, "");
  return {
    symbol,
    name: String(row.name || "").trim(),
    price,
    prev_close: numeric(row.settlement ?? row.prevclose),
    change_pct: numeric(row.changepercent),
    volume,
    amount: numeric(row.amount),
    turnover: numeric(row.turnoverratio),
    pe: numeric(row.per ?? row.pe_ratio),
    pb: numeric(row.pb),
    market_cap: market === "cn" || market === "etf" ? numeric(row.mktcap) * 10000 : numeric(row.market_value),
    float_market_cap: market === "cn" || market === "etf" ? numeric(row.nmc) * 10000 : 0,
    sector: "",
    industry: "",
  };
}

async function getSinaMarketPage(market, page, pageSize) {
  const common = `page=${page}&num=${pageSize}&sort=amount&asc=0&_s_r_a=page`;
  let url;
  if (market === "hk") {
    url = `https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHKStockData?${common}&node=qbgg_hk`;
  } else {
    const node = market === "etf" ? "etf_hq_fund" : "hs_a";
    url = `https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?${common}&node=${node}&symbol=`;
  }
  const text = await fetchText(url, {
    headers: { Referer: "https://finance.sina.com.cn" },
    encoding: "utf8",
  });
  const rows = JSON.parse(text);
  if (!Array.isArray(rows)) throw new Error("新浪市场列表返回格式错误");
  return rows
    .map((row) => normalizeSinaCandidate(row, market))
    .filter((row) => row.symbol && row.price > 0);
}

async function getSinaMarketCandidates(market, limit) {
  const pageSize = limit > 0 ? Math.max(20, Math.min(500, limit)) : 500;
  const rows = [];
  const seen = new Set();
  for (let page = 1; page <= 100; page += 1) {
    const batch = await getSinaMarketPage(market, page, pageSize);
    let added = 0;
    for (const row of batch) {
      if (seen.has(row.symbol)) continue;
      seen.add(row.symbol);
      rows.push(row);
      added += 1;
      if (limit > 0 && rows.length >= limit) break;
    }
    // Sina silently caps each page (currently about 100 for CN/ETF and 60 for
    // HK), even when num=500. Stop only at an empty/repeated page.
    if ((limit > 0 && rows.length >= limit) || batch.length === 0 || added === 0) break;
  }
  rows.sort((a, b) => b.amount - a.amount);
  return limit > 0 ? rows.slice(0, limit) : rows;
}

async function getNasdaqMarketCandidates(limit) {
  const url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true";
  const text = await fetchText(url, {
    headers: {
      Accept: "application/json, text/plain, */*",
      Origin: "https://www.nasdaq.com",
      Referer: "https://www.nasdaq.com/market-activity/stocks/screener",
    },
    encoding: "utf8",
  });
  const payload = JSON.parse(text);
  const rows = payload?.data?.rows;
  if (!Array.isArray(rows)) throw new Error("Nasdaq 市场列表返回格式错误");
  const candidates = rows.map((row) => {
    const price = numeric(row.lastsale);
    const volume = numeric(row.volume);
    return {
      symbol: String(row.symbol || "").trim().toUpperCase().replace("/", "."),
      name: String(row.name || "").trim(),
      price,
      prev_close: price - numeric(row.netchange),
      change_pct: numeric(row.pctchange),
      volume,
      amount: price * volume,
      turnover: 0,
      pe: 0,
      pb: 0,
      market_cap: numeric(row.marketCap),
      float_market_cap: 0,
      sector: String(row.sector || "").trim(),
      industry: String(row.industry || "").trim(),
    };
  })
    .filter((row) => row.symbol && row.price > 0)
    .sort((a, b) => b.amount - a.amount);
  return limit > 0 ? candidates.slice(0, limit) : candidates;
}

async function getMarketCandidates(market, limit = 0) {
  const normalized = String(market || "").toLowerCase();
  if (!["cn", "hk", "us", "etf"].includes(normalized)) throw new Error("不支持的市场");
  const requestedLimit = Number(limit);
  const boundedLimit = Number.isFinite(requestedLimit) && requestedLimit > 0
    ? Math.max(10, Math.min(50000, Math.trunc(requestedLimit)))
    : 0;
  const data = normalized === "us"
    ? await getNasdaqMarketCandidates(boundedLimit)
    : await getSinaMarketCandidates(normalized, boundedLimit);
  return {
    market: normalized,
    source: normalized === "us" ? "nasdaq-screener" : "sina-market-center",
    scope: boundedLimit > 0 ? "bounded" : "full-market",
    total_count: data.length,
    count: data.length,
    data,
  };
}

// ─── 自选列表 ──────────────────────────────────────────

function loadWatchlist() {
  try {
    return JSON.parse(fs.readFileSync(WATCHLIST_FILE, "utf8"));
  } catch {
    return [];
  }
}

function saveWatchlist(list) {
  fs.mkdirSync(path.dirname(WATCHLIST_FILE), { recursive: true });
  fs.writeFileSync(WATCHLIST_FILE, JSON.stringify(list, null, 2), "utf8");
}

// ─── 全套技术指标计算 ──────────────────────────────────

function sma(arr, n) {
  if (arr.length < n) return null;
  return +(arr.slice(-n).reduce((a, b) => a + b, 0) / n).toFixed(2);
}

function ema(arr, n) {
  if (arr.length < n) return null;
  const k = 2 / (n + 1);
  let result = arr.slice(0, n).reduce((a, b) => a + b, 0) / n;
  for (let i = n; i < arr.length; i++) {
    result = arr[i] * k + result * (1 - k);
  }
  return +result.toFixed(2);
}

function rsi(closes, n) {
  if (closes.length < n + 1) return null;
  let avgGain = 0, avgLoss = 0;
  const diffs = [];
  for (let i = closes.length - n; i < closes.length; i++) {
    diffs.push(closes[i] - closes[i - 1]);
  }
  for (const d of diffs) {
    if (d > 0) avgGain += d;
    else avgLoss += Math.abs(d);
  }
  avgGain /= n;
  avgLoss /= n;
  if (avgLoss === 0) return 100;
  return +(100 - 100 / (1 + avgGain / avgLoss)).toFixed(2);
}

function macd(closes) {
  if (closes.length < 26) return null;
  const ema12 = ema(closes, 12);
  const ema26 = ema(closes, 26);
  if (ema12 === null || ema26 === null) return null;
  const dif = +(ema12 - ema26).toFixed(4);
  const slices = [];
  for (let i = 26; i <= closes.length; i++) {
    const e12 = ema(closes.slice(0, i), 12);
    const e26 = ema(closes.slice(0, i), 26);
    if (e12 !== null && e26 !== null) slices.push(e12 - e26);
  }
  const dea = ema(slices, 9);
  const bar = dea !== null ? +((dif - dea) * 2).toFixed(4) : null;
  return { dif, dea, bar };
}

function boll(closes) {
  if (closes.length < 20) return null;
  const mid = sma(closes, 20);
  if (mid === null) return null;
  const slice = closes.slice(-20);
  const std = Math.sqrt(slice.reduce((s, c) => s + (c - mid) ** 2, 0) / 20);
  return {
    upper: +(mid + 2 * std).toFixed(2),
    mid: +mid.toFixed(2),
    lower: +(mid - 2 * std).toFixed(2),
    width: +((4 * std / mid) * 100).toFixed(2),
  };
}

function kdj(highs, lows, closes) {
  const n = 9;
  if (closes.length < n) return null;
  function calcKdjSlice(slice) {
    const lowestLow = Math.min(...slice.low);
    const highestHigh = Math.max(...slice.high);
    const rsv = ((slice.close - lowestLow) / (highestHigh - lowestLow)) * 100;
    return isNaN(rsv) ? 50 : rsv;
  }
  const rsvs = [];
  for (let i = n - 1; i < highs.length; i++) {
    rsvs.push(calcKdjSlice({
      high: highs.slice(i - n + 1, i + 1),
      low: lows.slice(i - n + 1, i + 1),
      close: closes[i],
    }));
  }
  let k = 50, d = 50;
  for (const rsv of rsvs) {
    k = (2 / 3) * k + (1 / 3) * rsv;
    d = (2 / 3) * d + (1 / 3) * k;
  }
  const j = 3 * k - 2 * d;
  return { k: +k.toFixed(2), d: +d.toFixed(2), j: +j.toFixed(2) };
}

function wr(highs, lows, closes) {
  const n = 14;
  if (highs.length < n) return null;
  const hh = Math.max(...highs.slice(-n));
  const ll = Math.min(...lows.slice(-n));
  const wrv = ((hh - closes[closes.length - 1]) / (hh - ll)) * 100;
  return +wrv.toFixed(2);
}

function atr(highs, lows, closes) {
  const n = 14;
  if (highs.length < n + 1) return null;
  const trs = [];
  for (let i = highs.length - n; i < highs.length; i++) {
    const h_l = highs[i] - lows[i];
    const h_pc = Math.abs(highs[i] - closes[i - 1]);
    const l_pc = Math.abs(lows[i] - closes[i - 1]);
    trs.push(Math.max(h_l, h_pc, l_pc));
  }
  const atrVal = trs.reduce((a, b) => a + b, 0) / n;
  return +atrVal.toFixed(2);
}

function obv(closes, volumes) {
  if (closes.length < 20) return null;
  const recent = closes.slice(-20);
  const recentVol = volumes.slice(-20);
  let obv = 0;
  for (let i = 1; i < recent.length; i++) {
    if (recent[i] > recent[i - 1]) obv += recentVol[i];
    else if (recent[i] < recent[i - 1]) obv -= recentVol[i];
  }
  return obv;
}

function avgVolume(volumes) {
  return {
    vol_ma5: sma(volumes, 5),
    vol_ma10: sma(volumes, 10),
    vol_ma20: sma(volumes, 20),
  };
}

function computeAllIndicators(klineData, realtime = null) {
  if (!klineData || klineData.length < 30) return { error: "数据不足，至少需要30个交易日" };

  const closes = klineData.map((d) => d.close);
  const highs = klineData.map((d) => d.high);
  const lows = klineData.map((d) => d.low);
  const opens = klineData.map((d) => d.open);
  const volumes = klineData.map((d) => d.volume);
  const price = closes[closes.length - 1];

  const mas = {
    ma5: sma(closes, 5),
    ma10: sma(closes, 10),
    ma20: sma(closes, 20),
    ma60: sma(closes, 60),
    ma120: sma(closes, 120),
    ma250: sma(closes, 250),
  };

  const maOrder = [mas.ma5, mas.ma10, mas.ma20].filter(v => v !== null);
  let maAlignment = "未知";
  if (maOrder.length === 3) {
    if (mas.ma5 > mas.ma10 && mas.ma10 > mas.ma20) maAlignment = "多头排列";
    else if (mas.ma5 < mas.ma10 && mas.ma10 < mas.ma20) maAlignment = "空头排列";
    else maAlignment = "交叉/缠绕";
  }

  const rsiValues = {
    rsi6: rsi(closes, 6),
    rsi14: rsi(closes, 14),
    rsi24: rsi(closes, 24),
  };

  const macdData = macd(closes);
  const bollData = boll(closes);
  const bollPosition = bollData
    ? price >= bollData.upper ? "突破上轨"
    : price >= bollData.mid ? "上轨与中轨之间"
    : price >= bollData.lower ? "中轨与下轨之间"
    : "跌破下轨"
    : null;

  const kdjData = kdj(highs, lows, closes);
  const wrData = wr(highs, lows, closes);
  const atrData = atr(highs, lows, closes);

  const volData = avgVolume(volumes);
  const latestVolume = volumes[volumes.length - 1];
  const volumeRatio = volData.vol_ma5
    ? +(latestVolume / volData.vol_ma5).toFixed(2)
    : null;

  const obvData = obv(closes, volumes);

  const high30 = +Math.max(...highs.slice(-30)).toFixed(2);
  const low30 = +Math.min(...lows.slice(-30)).toFixed(2);
  const pricePosition = +(((price - low30) / (high30 - low30)) * 100).toFixed(1);

  const bollMidVal = bollData ? bollData.mid : sma(closes, 20);
  const slice20 = closes.slice(-20);
  const std20 = Math.sqrt(slice20.reduce((s, c) => s + (c - bollMidVal) ** 2, 0) / 20);
  const volatility = +((std20 / bollMidVal) * 100).toFixed(2);

  let turnover = null;
  if (realtime && realtime.turnover) turnover = realtime.turnover;

  const change5d = closes.length >= 5
    ? +(((closes[closes.length - 1] - closes[closes.length - 5]) / closes[closes.length - 5]) * 100).toFixed(2)
    : null;
  const change10d = closes.length >= 10
    ? +(((closes[closes.length - 1] - closes[closes.length - 10]) / closes[closes.length - 10]) * 100).toFixed(2)
    : null;
  const change20d = closes.length >= 20
    ? +(((closes[closes.length - 1] - closes[closes.length - 20]) / closes[closes.length - 20]) * 100).toFixed(2)
    : null;

  return {
    mas,
    ma_alignment: maAlignment,
    rsi: rsiValues,
    macd: macdData,
    boll: bollData,
    boll_position: bollPosition,
    kdj: kdjData,
    wr: wrData,
    atr: atrData,
    volume: {
      latest: latestVolume,
      ma5: volData.vol_ma5,
      ma10: volData.vol_ma10,
      ma20: volData.vol_ma20,
      ratio: volumeRatio,
    },
    obv: obvData,
    price_range_30d: { high: high30, low: low30, position_pct: pricePosition },
    volatility,
    turnover,
    change: { d5: change5d, d10: change10d, d20: change20d },
  };
}

// ─── 主入口 ──────────────────────────────────────────────

async function main() {
  const args = process.argv.slice(2);
  const command = args[0];
  const input = args[1];
  const option = args[2];

  if (!command) {
    console.log(JSON.stringify({
      usage: {
        realtime: "node stock-fetcher.js realtime <代码>  (A股:600030/sh600030 港股:hk00700 美股:usAAPL ETF:sh510300)",
        history: "node stock-fetcher.js history <代码> [日K/周K/月K]",
        snapshot: "node stock-fetcher.js snapshot <代码>",
        search: "node stock-fetcher.js search <关键词>",
        market_list: "node stock-fetcher.js market-list <cn|hk|us|etf> [数量，0=全市场]",
        watchlist: "node stock-fetcher.js watchlist [add|remove <代码>]",
      },
    }));
    return;
  }

  // ── 全市场候选列表 ──
  if (command === "market-list") {
    if (!input) return console.log(JSON.stringify({ error: "请输入市场" }));
    const result = await getMarketCandidates(input, option === undefined ? 0 : Number(option));
    console.log(JSON.stringify(result));
    return;
  }

  // ── 搜索 ──
  if (command === "search") {
    if (!input) return console.log(JSON.stringify({ error: "请输入搜索关键词" }));
    const results = await searchStock(input);
    console.log(JSON.stringify(results));
    return;
  }

  // ── 自选列表 ──
  if (command === "watchlist" && input === "add") {
    const parsed = detectMarket(option);
    if (!parsed) return console.log(JSON.stringify({ error: "无效代码" }));
    const { market, code } = parsed;
    const tencentCode = toTencentCode(market, code);
    let list = loadWatchlist();
    if (list.find((s) => s.code === tencentCode)) {
      console.log(JSON.stringify({ message: "已在自选列表中", code: tencentCode }));
    } else {
      const rt = await getTencentRealtime(market, code);
      const name = rt.name || tencentCode;
      list.push({ code: tencentCode, name, market, addedAt: new Date().toISOString() });
      saveWatchlist(list);
      console.log(JSON.stringify({ message: "已添加", code: tencentCode, name, market, count: list.length }));
    }
    return;
  }

  if (command === "watchlist" && input === "remove") {
    const parsed = detectMarket(option);
    if (!parsed) return console.log(JSON.stringify({ error: "无效代码" }));
    const tencentCode = toTencentCode(parsed.market, parsed.code);
    let list = loadWatchlist();
    const before = list.length;
    list = list.filter((s) => s.code !== tencentCode);
    saveWatchlist(list);
    console.log(JSON.stringify({ message: before > list.length ? "已移除" : "未找到", code: tencentCode, count: list.length }));
    return;
  }

  if (command === "watchlist" && !input) {
    const list = loadWatchlist();
    if (list.length === 0) {
      console.log(JSON.stringify({ message: "自选列表为空", count: 0 }));
      return;
    }
    const results = [];
    for (const stock of list) {
      try {
        const parsed = detectMarket(stock.code);
        if (!parsed) throw new Error("无效代码");
        let data = await getTencentRealtime(parsed.market, parsed.code);
        results.push({
          code: stock.code,
          name: data.name || stock.name,
          market: stock.market || parsed.market,
          price: data.price,
          change: data.change,
          change_pct: data.change_pct,
          high: data.high,
          low: data.low,
        });
      } catch (e) {
        results.push({ code: stock.code, name: stock.name, error: e.message });
      }
    }
    console.log(JSON.stringify({ count: results.length, stocks: results }));
    return;
  }

  // ── 实时行情 ──
  if (command === "realtime") {
    if (!input) return console.log(JSON.stringify({ error: "请提供代码" }));
    const parsed = detectMarket(input);
    if (!parsed) return console.log(JSON.stringify({ error: "无效代码，示例：sh600519 / hk00700 / usAAPL / sh510300" }));
    let data;
    try {
      data = await getTencentRealtime(parsed.market, parsed.code);
      if (data.error) throw new Error(data.error);
    } catch (e) {
      // 腾讯失败 → A股走新浪备用
      if (parsed.market === "cn") {
        const sinaCode = parseSinaCode(parsed.code);
        data = await getSinaRealtime(sinaCode);
        data.change = data.price && data.prev_close
          ? +(data.price - data.prev_close).toFixed(2)
          : 0;
        data.change_pct = data.prev_close
          ? +((data.change / data.prev_close) * 100).toFixed(2)
          : 0;
      } else {
        console.log(JSON.stringify({ error: e.message }));
        return;
      }
    }
    if (data.name) {
      const sign = data.change >= 0 ? "+" : "";
      const unit = data.currency === "USD" ? "$" : "";
      data.display = `${data.name}(${data.code}) 价格:${unit}${data.price} ${sign}${data.change}(${sign}${data.change_pct}%) 最高:${data.high} 最低:${data.low} 昨收:${data.prev_close}`;
    }
    console.log(JSON.stringify(data));
    return;
  }

  // ── 历史K线 ──
  if (command === "history") {
    if (!input) return console.log(JSON.stringify({ error: "请提供代码" }));
    const parsed = detectMarket(input);
    if (!parsed) return console.log(JSON.stringify({ error: "无效代码" }));
    const period = (option === "weekly" || option === "monthly") ? option : "day";
    let data;
    try {
      data = await getTencentHistory(parsed.market, parsed.code, period === "day" ? "day" : "week", 1023, "qfq");
      if (period === "monthly") data = await getTencentHistory(parsed.market, parsed.code, "month", 1023, "qfq");
      if (data.error) throw new Error(data.error);
    } catch (e) {
      if (parsed.market === "cn") {
        const sinaCode = parseSinaCode(parsed.code);
        data = await getSinaHistory(sinaCode);
      } else {
        console.log(JSON.stringify({ error: e.message }));
        return;
      }
    }
    const allData = data.data || [];
    const requestedLimit = Number.parseInt(option, 10);
    const historyData = Number.isFinite(requestedLimit) && requestedLimit > 0
      ? allData.slice(-requestedLimit)
      : allData;
    const indicators = computeAllIndicators(allData);
    console.log(JSON.stringify({
      code: data.code,
      market: parsed.market,
      count: allData.length,
      data: historyData,
      indicators,
    }));
    return;
  }

  // ── 完整快照 ──
  if (command === "snapshot") {
    if (!input) return console.log(JSON.stringify({ error: "请提供代码" }));
    const parsed = detectMarket(input);
    if (!parsed) return console.log(JSON.stringify({ error: "无效代码" }));

    let realtime, history;
    try {
      realtime = await getTencentRealtime(parsed.market, parsed.code);
      history = await getTencentHistory(parsed.market, parsed.code, "day", 1023, "qfq");
      if (realtime.error) throw new Error(realtime.error);
      if (history.error) throw new Error(history.error);
    } catch (e) {
      if (parsed.market === "cn") {
        const sinaCode = parseSinaCode(parsed.code);
        realtime = await getSinaRealtime(sinaCode);
        history = await getSinaHistory(sinaCode);
        realtime.change = realtime.price && realtime.prev_close
          ? +(realtime.price - realtime.prev_close).toFixed(2)
          : 0;
        realtime.change_pct = realtime.prev_close
          ? +((realtime.change / realtime.prev_close) * 100).toFixed(2)
          : 0;
      } else {
        console.log(JSON.stringify({ error: e.message }));
        return;
      }
    }

    const allData = history.data || [];
    const indicators = computeAllIndicators(allData, realtime);
    const snapshot = {
      realtime,
      // The staged Agent graph needs enough bars to independently verify
      // medium-term trend, volatility and outcome reflections.
      history: allData.slice(-60),
      indicators,
    };
    console.log(JSON.stringify(snapshot));
    return;
  }

  console.log(JSON.stringify({ error: `未知命令: ${command}` }));
}

if (require.main === module) {
  main().catch((e) => {
    console.error(JSON.stringify({ error: e.message }));
    process.exit(1);
  });
}

module.exports = { detectMarket };
