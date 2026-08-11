#!/usr/bin/env node
/**
 * 宏观环境新闻采集脚本
 * 用法: node collect.js <daily|weekly|monthly|yearly> [--date YYYY-MM-DD]
 *
 * 数据源：
 *   主源：新浪财经 Feed API（5频道 × 5页/频道）
 *   降级：同花顺 7×24 快讯 API（新浪故障时自动切换）
 *   行情：东方财富 push2 API
 */

const https = require('https');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFile } = require('child_process');

// ========================= 配置 =========================

const DATA_ROOT = process.env.MACRO_DATA_DIR
  ? path.resolve(process.env.MACRO_DATA_DIR)
  : path.resolve(__dirname, '..', '..', 'runtime', 'macro');
const NEWS_DIR = path.join(DATA_ROOT, 'news');
const MARKS_FILE = path.join(DATA_ROOT, 'marks.json');

// 新浪财经频道配置
// 注意: pageid=155 lid=1687（宏观经济）更新极慢（数月前的研报），不适合日报
// 改用 pageid=153 下高频频道覆盖
const CHANNELS = [
  { pageid: 153, lid: 2517, channel: 'market',        label: '财经综合' },   // 国内宏观/政策/市场
  { pageid: 153, lid: 2516, channel: 'international', label: '国际财经' },   // 美联储/地缘/大宗
  { pageid: 153, lid: 2515, channel: 'industry',      label: '科技产业' },   // 行业动态/科技
  { pageid: 153, lid: 2514, channel: 'geopolitics',   label: '国际时政' },   // 地缘政治/贸易
  { pageid: 153, lid: 2509, channel: 'market2',       label: '财经深度' },   // 深度分析/政策解读
];

const MAX_PAGES = 5;
const PAGE_SIZE = 50; // 每页50条，5页=250条/频道，确保覆盖完整一天数据

// 分类关键词规则
const CATEGORY_RULES = [
  { category: '货币政策', keywords: ['央行','MLF','LPR','降准','降息','逆回购','SLF','PSL','准备金','货币政策','公开市场','利率走廊','OMO'] },
  { category: '财政政策', keywords: ['财政部','专项债','国债','减税','赤字','转移支付','特别国债','财政'] },
  { category: '产业政策', keywords: ['发改委','工信部','规划','产业政策','补贴','准入','目录','新质生产力','新型工业化'] },
  { category: '监管动态', keywords: ['证监会','银保监会','金融监管','交易所','问询','立案','处罚','退市','IPO','注册制','信息披露'] },
  { category: '宏观数据', keywords: ['GDP','PMI','CPI','PPI','社融','M2','进出口','固定资产投资','社会消费品','工业增加值','失业率','外汇储备','贸易顺差'] },
  { category: '国际财经', keywords: ['美联储','加息','降息','非农','美债','美元','日元','欧元','欧央行','IMF','世界银行','WTO'] },
  { category: '大宗商品', keywords: ['原油','黄金','铜','铁矿石','锂','稀土','煤炭','天然气','OPEC'] },
  { category: '地缘政治', keywords: ['制裁','贸易战','关税','地缘','冲突','军事','战争','脱钩','封锁','联盟'] },
  { category: '行业动态', keywords: ['行业','板块','产业链','产能','涨价','降价','供需','库存','景气'] },
  { category: '科技前沿', keywords: ['AI','人工智能','芯片','半导体','量子','5G','6G','新能源','光伏','储能','自动驾驶','机器人'] },
];

// 行业板块映射
const SECTOR_KEYWORDS = [
  { sector: '半导体',   keywords: ['半导体','芯片','集成电路','晶圆','光刻','封装测试','EDA'] },
  { sector: '新能源',   keywords: ['新能源','光伏','风电','储能','锂电池','固态电池','氢能','充电桩'] },
  { sector: '新能源汽车', keywords: ['新能源汽车','电动汽车','新能源车','自动驾驶','智能汽车','造车新势力'] },
  { sector: '消费电子', keywords: ['消费电子','手机','PC','智能穿戴','AR','VR','MR','TWS'] },
  { sector: '医药生物', keywords: ['医药','生物制药','创新药','医疗器械','仿制药','CRO','CDMO','中药','疫苗'] },
  { sector: '金融',     keywords: ['银行','券商','保险','信托','金融科技','数字货币'] },
  { sector: '房地产',   keywords: ['房地产','楼市','房','限购','房贷','保障房','城市更新','旧改'] },
  { sector: '人工智能', keywords: ['AI','人工智能','大模型','机器学习','深度学习','NLP','计算机视觉','AIGC'] },
  { sector: '国防军工', keywords: ['军工','航天','航空发动机','舰船','导弹','雷达'] },
  { sector: '有色金属', keywords: ['有色','铜','铝','锂','钴','镍','稀土'] },
  { sector: '食品饮料', keywords: ['白酒','食品','饮料','乳业','调味品','预制菜'] },
  { sector: '电力',     keywords: ['电力','电网','火电','水电','核电','特高压','虚拟电厂'] },
  { sector: '通信',     keywords: ['5G','6G','光通信','卫星通信','光模块','算力','数据中心'] },
  { sector: '交通运输', keywords: ['航空','铁路','航运','港口','物流','快递'] },
  { sector: '建筑装饰', keywords: ['基建','水利','建筑','工程','城镇化','海绵城市'] },
];

// ========================= 工具函数 =========================

const HTTP_HEADERS = {
  'User-Agent': 'Mozilla/5.0 (compatible; InvestmentAuto/0.2)',
  'Accept': 'application/json,text/plain,*/*',
};

function consumeResponse(response, url, timeoutMs, redirectsLeft, request, resolve, reject) {
  const status = response.statusCode || 0;
  if (status >= 300 && status < 400 && response.headers.location) {
    response.resume();
    if (redirectsLeft <= 0) return reject(new Error('too many redirects'));
    const target = new URL(response.headers.location, url).toString();
    return httpGet(target, timeoutMs, redirectsLeft - 1).then(resolve, reject);
  }
  if (status < 200 || status >= 300) {
    response.resume();
    return reject(new Error(`HTTP ${status}`));
  }
  const chunks = [];
  let size = 0;
  response.on('data', (chunk) => {
    size += chunk.length;
    if (size > 20 * 1024 * 1024) {
      request.destroy(new Error('response too large'));
      return;
    }
    chunks.push(chunk);
  });
  response.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
}

function directHttpGet(url, timeoutMs, redirectsLeft) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, { headers: HTTP_HEADERS }, (response) => {
      consumeResponse(response, url, timeoutMs, redirectsLeft, request, resolve, reject);
    });
    request.setTimeout(timeoutMs, () => request.destroy(new Error('request timeout')));
    request.on('error', reject);
  });
}

function curlHttpGet(url, timeoutMs) {
  return new Promise((resolve, reject) => {
    execFile('curl', [
      '-L', '--silent', '--show-error',
      '--max-time', String(Math.ceil(timeoutMs / 1000)),
      '--connect-timeout', '5',
      '-H', HTTP_HEADERS['User-Agent'],
      url,
    ], { encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 }, (error, stdout, stderr) => {
      if (error) return reject(new Error(stderr?.trim() || error.message || 'curl request failed'));
      resolve(stdout);
    });
  });
}

function httpGet(url, timeoutMs = 8000, redirectsLeft = 4) {
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy;
  if (proxy) return curlHttpGet(url, timeoutMs);
  return directHttpGet(url, timeoutMs, redirectsLeft).catch((nativeError) =>
    curlHttpGet(url, timeoutMs).catch(() => { throw nativeError; })
  );
}

function httpGetJSON(url) {
  return httpGet(url).then(data => JSON.parse(data));
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function md5(str) {
  return crypto.createHash('md5').update(str).digest('hex');
}

function parseDate(dateStr) {
  // 解析 YYYY-MM-DD
  const [y, m, d] = dateStr.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function formatDate(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function getWeekNumber(d) {
  const date = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  date.setUTCDate(date.getUTCDate() + 4 - (date.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const weekNo = Math.ceil(((date - yearStart) / 86400000 + 1) / 7);
  return `${date.getUTCFullYear()}-W${String(weekNo).padStart(2, '0')}`;
}

// ========================= 分类逻辑 =========================

function classifyNews(title, intro) {
  const text = `${title} ${intro}`;
  const categories = [];

  for (const rule of CATEGORY_RULES) {
    for (const kw of rule.keywords) {
      if (text.includes(kw)) {
        categories.push(rule.category);
        break;
      }
    }
  }

  // 默认分类
  if (categories.length === 0) {
    categories.push('其他');
  }

  // 检测关联行业
  const sectors = [];
  for (const rule of SECTOR_KEYWORDS) {
    for (const kw of rule.keywords) {
      if (text.includes(kw)) {
        sectors.push(rule.sector);
        break;
      }
    }
  }

  return { categories, sectors };
}

function judgeImportance(title, intro, mediaName) {
  const text = `${title} ${intro}`;

  // 高重要性关键词 — 国内政策/宏观/国际重大事件
  const highKeywords = [
    '央行', 'MLF', 'LPR', '降准', '降息', '美联储', '加息', '欧央行',
    '战争', '制裁', '制裁清单', '贸易战', '关税',
    '立案', '被查', '落马', '审查', '处分',
    'GDP', 'PMI', 'CPI', 'PPI', '社融', 'M2',
    '重大', '突发', '紧急', '重磅',
    '证监会', '发改委', '财政部', '工信部',
    '油价', '原油', '布伦特',
    '美债', '收益率', '国债',
    '冲突', '袭击', '空袭', '军事',
  ];

  // 国内核心来源
  const eliteSources = ['央行', '财政部', '发改委', '证监会', '新华社', '人民日报', '央视'];

  // 低重要性：美股日常/个股层面
  const lowKeywords = [
    '季度股息', '宣布派息', '股票回购', '分红',
    '第二季度净利润', '第二季度业绩', '二季度业绩', '第二季度营收',
    'Q2盈利', 'Q2业绩', 'Q2营收', 'Q2净利润',
    '目标价', '评级', '上调', '下调',
    'IPO', '首次公开募股',
    '董事会', '任命', '高管',
    '盘前', '盘后', '早盘', '收盘',
    '买入评级', '卖出评级', '持有评级',
    '宣布将', '定于', '即将发布',
    '宣布季度', '公布第二季度',
  ];

  // 纯播报/公告类
  const lowTitlePatterns = [
    /^(美股|盘前|开盘|收盘|早盘|午盘).*?(涨跌|不一)/,
    /^(收评|午评)/,
    /(公告速递|公司公告|一周回顾|隔夜要闻)/,
  ];

  // 高重要性评分
  let score = 1; // 默认 medium

  // 国内核心来源 + 高关键词 → high
  if (eliteSources.some(src => mediaName.includes(src))) {
    score = Math.max(score, highKeywords.some(kw => text.includes(kw)) ? 2 : 1);
  }

  // 高关键词命中 → high
  if (highKeywords.some(kw => text.includes(kw))) score = 2;

  // 低质关键词 → low（除非有高关键词覆盖）
  if (score < 2 && lowKeywords.some(kw => text.includes(kw))) score = 0;

  // 低质标题模式 → low
  if (lowTitlePatterns.some(p => p.test(title))) score = 0;

  return score === 2 ? 'high' : score === 0 ? 'low' : 'medium';
}

// ========================= 采集 =========================

async function fetchChannel(channel, pageCount) {
  const allNews = [];

  for (let page = 1; page <= pageCount; page++) {
    const url = `https://feed.mix.sina.com.cn/api/roll/get?pageid=${channel.pageid}&lid=${channel.lid}&k=&num=${PAGE_SIZE}&page=${page}`;
    try {
      const resp = await httpGetJSON(url);
      const data = resp?.result?.data || [];

      if (data.length === 0) break;

      for (const item of data) {
        const timestamp = item.ctime || item.intime;
        const { categories, sectors } = classifyNews(item.title || '', item.intro || '');

        allNews.push({
          id: md5(item.docid || item.url || item.title),
          time: timestamp,
          date: formatDate(new Date(timestamp * 1000)),
          title: item.title || '',
          intro: item.intro || '',
          summary: item.summary || '',
          source: item.media_name || '未知来源',
          url: item.url || '',
          channel: channel.channel,
          categories,
          sectors,
          importance: judgeImportance(item.title || '', item.intro || '', item.media_name || ''),
          tags: extractTags(item.title || '', item.intro || ''),
        });
      }

      if (data.length < PAGE_SIZE) break; // 最后一页
      await sleep(1000); // 请求间隔
    } catch (err) {
      console.error(`  获取 ${channel.label} 第${page}页失败:`, err.message);
      break;
    }
  }

  return allNews;
}

// ========================= 同花顺降级源 =========================

const THS_PAGES = 10; // 同花顺每页20条，10页=200条，覆盖晚报一天

async function fetch10jqka(pageCount, timeWindow) {
  const allNews = [];
  const fallbackTimestamp = Math.floor((timeWindow.start + timeWindow.end) / 2); // 无时间字段时归入目标日报窗口中点

  for (let page = 1; page <= pageCount; page++) {
    const url = `https://news.10jqka.com.cn/tapp/news/push/stock/?page=${page}&tag=&track=website&ctgid=0`;
    try {
      const resp = await httpGetJSON(url);
      const list = resp?.data?.list || [];

      if (list.length === 0) break;

      for (const item of list) {
        const title = item.title || '';
        const intro = item.digest || '';
        // 同花顺 seq 是新闻序列号，不是 Unix 时间戳；优先使用真实时间字段，缺失时归入目标窗口中点
        const rawTime = item.ctime || item.time || item.publish_time || item.publishTime || item.showTime || item.date;
        const parsedDate = rawTime ? new Date(String(rawTime).replace(/-/g, '/')) : null;
        const timestamp = parsedDate && !isNaN(parsedDate.getTime())
          ? Math.floor(parsedDate.getTime() / 1000)
          : fallbackTimestamp;
        const { categories, sectors } = classifyNews(title, intro);

        allNews.push({
          id: md5(item.id || title),
          time: timestamp,
          date: formatDate(new Date(timestamp * 1000)),
          title,
          intro,
          summary: intro.slice(0, 200),
          source: '同花顺',
          url: item.url || '',
          channel: 'ths_kuaixun',
          categories,
          sectors,
          importance: judgeImportance(title, intro, '同花顺'),
          tags: extractTags(title, intro),
        });
      }

      if (list.length < 20) break; // 同花顺每页20条
      await sleep(500);
    } catch (err) {
      console.error(`  同花顺第${page}页失败:`, err.message);
      break;
    }
  }

  return allNews;
}

// ========================= 主采集入口（带降级）=========================

async function collectAllNews(pageCount, timeWindow) {
  // 先尝试新浪
  const channelResults = await Promise.all(
    CHANNELS.map(ch => fetchChannel(ch, pageCount))
  );

  let allNews = channelResults.flat();
  console.error(`📥 新浪采集 ${allNews.length} 条`);

  // 如果新浪采集失败（所有频道都返回空），降级到同花顺
  if (allNews.length === 0) {
    console.error('⚠️  新浪采集为空，降级到同花顺...');
    allNews = await fetch10jqka(THS_PAGES, timeWindow);
    console.error(`📥 同花顺降级采集 ${allNews.length} 条`);
  }

  return allNews;
}

function extractTags(title, intro) {
  const text = title + ' ' + intro;
  const tagPatterns = [
    /(?:MLF|LPR|逆回购|SLF|PSL)/g,
    /(?:降准|降息|加息)/g,
    /(?:GDP|PMI|CPI|PPI|社融)/g,
    /(?:半导体|芯片|AI|人工智能|大模型)/g,
    /(?:新能源|光伏|储能|锂电池)/g,
    /(?:美联储|欧央行|央行)/g,
  ];
  const tags = new Set();
  for (const pattern of tagPatterns) {
    const matches = text.match(pattern);
    if (matches) matches.forEach(m => tags.add(m));
  }
  return [...tags].slice(0, 5);
}

function deduplicate(newsList) {
  const seen = new Set();
  const result = [];

  for (const news of newsList) {
    const key = `${news.title}|${news.source}`;
    const hash = md5(key);

    if (seen.has(hash)) continue;

    // 相似标题去重
    const similar = result.find(r => {
      const overlap = similarity(r.title, news.title);
      return overlap > 0.7;
    });

    if (similar) {
      // 保留 intro 更长的版本
      if (news.intro.length > similar.intro.length) {
        Object.assign(similar, news);
      }
      continue;
    }

    seen.add(hash);
    result.push(news);
  }

  return result;
}

function similarity(a, b) {
  const wordsA = new Set(a.slice(0, 60).split(''));
  const wordsB = new Set(b.slice(0, 60).split(''));
  const intersection = [...wordsA].filter(x => wordsB.has(x)).length;
  const union = wordsA.size + wordsB.size - intersection;
  return union === 0 ? 0 : intersection / union;
}

// ========================= 东方财富行情 =========================

async function fetchMarketData() {
  try {
    // 大盘指数
    const indexResp = await httpGetJSON(
      'https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001,0.399001,1.000688,0.399006&fields=f2,f3,f4,f5,f6,f12,f14'
    );

    // 行业板块涨跌 TOP10
    const sectorResp = await httpGetJSON(
      'https://push2.eastmoney.com/api/qt/clist/get?cb=&fid=f3&po=1&pz=10&pn=1&np=1&fltt=2&fs=m:90+t:2&fields=f2,f3,f4,f12,f14'
    );

    const sectorBottom = await httpGetJSON(
      'https://push2.eastmoney.com/api/qt/clist/get?cb=&fid=f3&po=0&pz=10&pn=1&np=1&fltt=2&fs=m:90+t:2&fields=f2,f3,f4,f12,f14'
    );

    const indices = (indexResp?.data?.diff || []).map(item => ({
      code: item.f12,
      name: item.f14,
      price: item.f2,
      change_pct: item.f3,
      change_val: item.f4,
      volume: item.f5,
      amount: item.f6,
    }));

    const topSectors = (sectorResp?.data?.diff || []).map(item => ({
      code: item.f12,
      name: item.f14,
      price: item.f2,
      change_pct: item.f3,
    }));

    const bottomSectors = (sectorBottom?.data?.diff || []).map(item => ({
      code: item.f12,
      name: item.f14,
      price: item.f2,
      change_pct: item.f3,
    }));

    return { indices, topSectors, bottomSectors };
  } catch (err) {
    console.error('  获取行情数据失败:', err.message);
    return { indices: [], topSectors: [], bottomSectors: [] };
  }
}

// ========================= 主流程 =========================

function getTimeWindow(period, refDate) {
  // 所有报告均面向"已完成的时间周期"：
  // daily  = 昨天
  // weekly = 上周（周一~周日）
  // monthly = 上个月
  // yearly  = 去年
  let start, end, startDate, endDate;

  switch (period) {
    case 'daily': {
      // 昨天 00:00 ~ 23:59:59
      end = new Date(refDate);
      end.setHours(0, 0, 0, 0);
      end.setSeconds(-1); // 昨天 23:59:59
      start = new Date(end);
      start.setHours(0, 0, 0, 0);
      break;
    }
    case 'weekly': {
      // 上周一 00:00 ~ 上周日 23:59:59
      const today = new Date(refDate);
      const dayOfWeek = today.getDay(); // 0=周日
      const daysSinceMonday = dayOfWeek === 0 ? 6 : dayOfWeek - 1;
      // 本周一
      const thisMonday = new Date(today);
      thisMonday.setDate(today.getDate() - daysSinceMonday);
      thisMonday.setHours(0, 0, 0, 0);
      // 上周日 = 本周一 - 1 天
      end = new Date(thisMonday.getTime() - 1000);
      // 上周一 = 本周一 - 7 天
      start = new Date(thisMonday.getTime() - 7 * 86400000);
      start.setHours(0, 0, 0, 0);
      break;
    }
    case 'monthly': {
      // 上月1号 ~ 上月最后一天
      const now = new Date(refDate);
      // 本月1号 00:00:00
      const thisMonth1st = new Date(now.getFullYear(), now.getMonth(), 1);
      // 上月最后一天 23:59:59 = 本月1号 - 1秒
      end = new Date(thisMonth1st.getTime() - 1000);
      // 上月1号
      start = new Date(end.getFullYear(), end.getMonth(), 1);
      break;
    }
    case 'yearly': {
      // 去年1月1日 ~ 去年12月31日
      const thisYear = new Date(refDate).getFullYear();
      start = new Date(thisYear - 1, 0, 1);
      end = new Date(thisYear - 1, 11, 31, 23, 59, 59);
      break;
    }
    default:
      start = new Date(refDate);
      start.setDate(start.getDate() - 1);
      start.setHours(0, 0, 0, 0);
      end = new Date(start);
      end.setHours(23, 59, 59, 999);
  }

  return {
    start: Math.floor(start.getTime() / 1000),
    end: Math.floor(end.getTime() / 1000),
    startDate: formatDate(start),
    endDate: formatDate(end),
  };
}

async function main() {
  const args = process.argv.slice(2);
  const period = args[0] || 'daily';
  const dateIdx = args.indexOf('--date');
  const refDate = dateIdx !== -1 ? parseDate(args[dateIdx + 1]) : new Date();

  if (!['daily', 'weekly', 'monthly', 'yearly'].includes(period)) {
    console.error('用法: node collect.js <daily|weekly|monthly|yearly> [--date YYYY-MM-DD]');
    process.exit(1);
  }

  const window = getTimeWindow(period, refDate);
  console.error(`📡 采集 ${period} 新闻: ${window.startDate} ~ ${window.endDate}`);

  // 日报也需要翻多页（昨天数据可能被今天新数据挤到后面）
  // weekly/monthly 也是 MAX_PAGES，确保覆盖完整窗口
  const pageCount = MAX_PAGES;
  console.error(`📋 遍历 ${CHANNELS.length} 个频道，每频道最多 ${pageCount} 页`);

  let allNews = await collectAllNews(pageCount, window);
  console.error(`📥 原始采集 ${allNews.length} 条`);

  // 时间筛选
  allNews = allNews.filter(n => {
    const t = Number(n.time);
    return !isNaN(t) && t >= window.start && t <= window.end;
  });
  console.error(`⏱  时间筛选后 ${allNews.length} 条`);

  if (allNews.length === 0) {
    console.error('❌ 新闻采集为空：主源和降级源均未得到目标时间窗口内新闻，停止生成空日报');
    process.exit(2);
  }

  // 去重
  allNews = deduplicate(allNews);
  console.error(`🔍 去重后 ${allNews.length} 条`);

  // 按重要性排序 + 国内优先
  const importanceOrder = { high: 0, medium: 1, low: 2 };

  // 国内核心来源的优先级高于同级别其他来源
  const domesticElite = ['央行', '财政部', '发改委', '证监会', '新华社', '人民日报', '央视'];
  const domesticSources = ['21世纪经济报道', '每日经济新闻', '第一财经', '上海证券报', '证券时报',
    '经济观察报', '澎湃新闻', '界面', '券商中国', '新浪证券', '一财网'];

  function sourcePriority(news) {
    const s = news.source || '';
    if (domesticElite.some(e => s.includes(e))) return 0;
    if (domesticSources.some(d => s.includes(d))) return 1;
    return 2; // 国际/其他
  }

  allNews.sort((a, b) => {
    const impDiff = importanceOrder[a.importance] - importanceOrder[b.importance];
    if (impDiff !== 0) return impDiff;
    // 同重要性：国内优先
    const srcDiff = sourcePriority(a) - sourcePriority(b);
    if (srcDiff !== 0) return srcDiff;
    return b.time - a.time;
  });

  // 获取行情数据（仅日报/周报需要，且仅在交易日获取）
  let marketData = null;
  if (period === 'daily' || period === 'weekly') {
    const targetDate = period === 'daily'
      ? new Date(window.end * 1000)
      : new Date(window.end * 1000);
    const dayOfWeek = targetDate.getDay();
    const isWeekend = dayOfWeek === 0 || dayOfWeek === 6;
    if (isWeekend) {
      console.error('📴 周末（非交易日），跳过行情数据采集');
      marketData = { indices: [], topSectors: [], bottomSectors: [], _isWeekend: true, _date: formatDate(targetDate) };
    } else {
      marketData = await fetchMarketData();
    }
  }

  // 统计
  const stats = {
    total: allNews.length,
    high: allNews.filter(n => n.importance === 'high').length,
    medium: allNews.filter(n => n.importance === 'medium').length,
    low: allNews.filter(n => n.importance === 'low').length,
    categories: {},
    sectors: {},
  };

  for (const n of allNews) {
    for (const c of n.categories) {
      stats.categories[c] = (stats.categories[c] || 0) + 1;
    }
    for (const s of n.sectors) {
      stats.sectors[s] = (stats.sectors[s] || 0) + 1;
    }
  }

  const result = {
    period,
    window,
    collected_at: new Date().toISOString(),
    stats,
    news: allNews,
    market_data: marketData,
  };

  // 保存到文件（日报用"昨天"日期，月报用上月月份，年报用去年年份）
  let fileKey;
  if (period === 'daily') {
    fileKey = window.endDate; // 昨天的日期
  } else if (period === 'weekly') {
    fileKey = `${window.startDate}_${window.endDate}`;
  } else if (period === 'monthly') {
    fileKey = window.startDate.slice(0, 7); // 上月 YYYY-MM
  } else {
    fileKey = String(new Date(window.start * 1000).getFullYear()); // 去年
  }
  const outputFile = path.join(NEWS_DIR, `${fileKey}.json`);
  fs.writeFileSync(outputFile, JSON.stringify(result, null, 2));
  console.error(`💾 已保存到 ${outputFile}`);

  // 同时输出 JSON 到 stdout 供管道使用
  console.log(JSON.stringify(result));
}

main().catch(err => {
  console.error('采集失败:', err);
  process.exit(1);
});
