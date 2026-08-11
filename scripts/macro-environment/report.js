#!/usr/bin/env node
/**
 * 宏观环境报告生成脚本
 * 用法: node report.js <daily|weekly|monthly|yearly> [--date YYYY-MM-DD]
 *
 * 读取采集的 JSON 数据，生成 Markdown 报告，输出到 stdout 和存档文件。
 */

const fs = require('fs');
const path = require('path');

// ========================= 配置 =========================

const REPORTS_DIR = process.env.MACRO_DATA_DIR
  ? path.resolve(process.env.MACRO_DATA_DIR)
  : path.resolve(__dirname, '..', '..', 'runtime', 'macro');
const NEWS_DIR = path.join(REPORTS_DIR, 'news');
const MARKS_FILE = path.join(REPORTS_DIR, 'marks.json');

const PERIOD_DIRS = {
  daily: 'daily',
  weekly: 'weekly',
  monthly: 'monthly',
  yearly: 'yearly',
};

// 周几中文
const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];

function getWeekNumber(d) {
  const date = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  date.setUTCDate(date.getUTCDate() + 4 - (date.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const weekNo = Math.ceil(((date - yearStart) / 86400000 + 1) / 7);
  return `${date.getUTCFullYear()}-W${String(weekNo).padStart(2, '0')}`;
}

// ========================= 渲染辅助 =========================

function formatTime(ts) {
  const d = new Date(ts * 1000);
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const h = String(d.getHours()).padStart(2, '0');
  const min = String(d.getMinutes()).padStart(2, '0');
  return `${m}-${day} ${h}:${min}`;
}

function importanceEmoji(level) {
  return level === 'high' ? '🔴' : level === 'medium' ? '🟡' : '⚪';
}

function categoryEmoji(cat) {
  const map = {
    '货币政策': '🏦',
    '财政政策': '💰',
    '产业政策': '🏭',
    '监管动态': '⚖️',
    '宏观数据': '📊',
    '国际财经': '🌍',
    '大宗商品': '🛢️',
    '地缘政治': '⚔️',
    '行业动态': '📈',
    '科技前沿': '🤖',
  };
  return map[cat] || '📌';
}

function generateId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

// ========================= 日报 =========================

function generateDaily(data) {
  // 日报面向"昨天"，所以 endDate 就是昨天
  const date = data.window.endDate;
  const d = new Date(date);
  const weekday = WEEKDAYS[d.getDay()];
  const news = data.news || [];
  const market = data.market_data || {};

  const high = news.filter(n => n.importance === 'high');
  const medium = news.filter(n => n.importance === 'medium');
  const low = news.filter(n => n.importance === 'low');

  let report = '';

  // Header - 显示为"昨日"
  report += `## 📰 宏观日报 | 昨日 ${date}（周${weekday}）\n\n`;

  // 今日概览
  report += `> 📡 采集 ${data.stats.total} 条新闻 | 🔴 高 ${data.stats.high} | 🟡 中 ${data.stats.medium} | ⚪ 低 ${data.stats.low}\n\n`;

  // Highlight
  if (high.length > 0) {
    report += `### ⚡ 重点关注\n\n`;
    for (const n of high.slice(0, 8)) {
      const cats = n.categories.map(c => `${categoryEmoji(c)}${c}`).join(' ');
      report += `**${importanceEmoji(n.importance)} ${n.title}**\n`;
      report += `> ${cats} | ${n.source} | ${formatTime(n.time)}\n`;
      if (n.intro) report += `> ${n.intro.slice(0, 120)}\n`;
      report += `> 📎 [原文](${n.url})\n\n`;
    }
    report += `\n`;
  }

  // 分类展示（中重要度+）
  const groupedByCategory = {};
  for (const n of [...high, ...medium]) {
    const cat = n.categories[0] || '其他';
    if (!groupedByCategory[cat]) groupedByCategory[cat] = [];
    if (groupedByCategory[cat].length < 5) groupedByCategory[cat].push(n);
  }

  const categoryOrder = ['货币政策', '财政政策', '产业政策', '监管动态', '宏观数据', '行业动态', '科技前沿', '大宗商品', '国际财经', '地缘政治', '其他'];

  for (const cat of categoryOrder) {
    const items = groupedByCategory[cat];
    if (!items || items.length === 0) continue;
    report += `### ${categoryEmoji(cat)} ${cat}\n\n`;
    for (const n of items) {
      report += `- ${importanceEmoji(n.importance)} **[${n.title}](${n.url})** — ${n.source}\n`;
    }
    report += `\n`;
  }

  // 行情数据
  const indices = market.indices || [];
  if (market._isWeekend) {
    report += `### 📊 行情数据\n\n`;
    report += `> ⚠️ 昨日为周末，A股休市。以下为最近交易日数据，具体请查阅前一个交易日日报。\n\n`;
  } else if (indices.length > 0) {
    report += `### 📊 行情数据\n\n`;
    report += `| 指数 | 收盘 | 涨跌幅 |\n`;
    report += `|------|------|--------|\n`;
    for (const idx of indices) {
      const sign = idx.change_pct >= 0 ? '+' : '';
      report += `| ${idx.name} | ${idx.price} | ${sign}${idx.change_pct}% |\n`;
    }
    report += `\n`;
  }

  const topSectors = market.topSectors || [];
  const bottomSectors = market.bottomSectors || [];
  if (topSectors.length > 0) {
    report += `**📈 板块涨跌 TOP5**\n\n`;
    report += `🏆 涨幅: ${topSectors.slice(0, 5).map(s => `${s.name}(+${s.change_pct}%)`).join(' | ')}\n`;
    report += `💔 跌幅: ${bottomSectors.slice(0, 5).map(s => `${s.name}(${s.change_pct}%)`).join(' | ')}\n`;
    report += `\n`;
  }

  // 今日关键词
  const allTags = {};
  for (const n of news) {
    for (const t of n.tags || []) {
      allTags[t] = (allTags[t] || 0) + 1;
    }
  }
  const topTags = Object.entries(allTags)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([t]) => `#${t}`);

  if (topTags.length > 0) {
    report += `💡 ${topTags.join(' ')}\n\n`;
  }

  report += `---\n*报告生成时间: ${new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}*\n`;

  return report;
}

// ========================= 周报 =========================

function generateWeekly(data) {
  const endDate = data.window.endDate;
  const startDate = data.window.startDate;
  const wn = getWeekNumber(new Date(endDate));
  const news = data.news || [];
  const high = news.filter(n => n.importance === 'high');
  const medium = news.filter(n => n.importance === 'medium');

  let report = '';

  report += `## 📅 宏观周报 | ${startDate} ~ ${endDate}（${wn}）\n\n`;
  report += `> 📡 本周采集 ${data.stats.total} 条 | 🔴 高 ${data.stats.high} | 🟡 中 ${data.stats.medium}\n\n`;

  // 本周主线
  if (high.length > 0) {
    report += `### 🔥 本周主线\n\n`;
    for (const n of high.slice(0, 15)) {
      report += `- ${importanceEmoji(n.importance)} **[${n.title}](${n.url})** — ${n.source} ${formatTime(n.time)}\n`;
    }
    report += `\n`;
  }

  // 分类汇总
  report += `### 📋 分类汇总\n\n`;
  const cats = Object.entries(data.stats.categories || {}).sort((a, b) => b[1] - a[1]);
  for (const [cat, count] of cats) {
    const bar = '█'.repeat(Math.min(count, 20));
    report += `- ${categoryEmoji(cat)} ${cat}: ${count} 条 ${bar}\n`;
  }
  report += `\n`;

  // 行业热度
  report += `### 🏭 行业热度\n\n`;
  const sectors = Object.entries(data.stats.sectors || {}).sort((a, b) => b[1] - a[1]);
  for (const [sector, count] of sectors.slice(0, 10)) {
    report += `- ${sector}: ${count} 条相关新闻\n`;
  }
  report += `\n`;

  // 本周重要数据
  report += `### 📊 本周重要数据发布\n\n`;
  const dataNews = news.filter(n => n.categories.includes('宏观数据'));
  if (dataNews.length > 0) {
    for (const n of dataNews) {
      report += `- [${n.title}](${n.url})\n`;
    }
  } else {
    report += `（本周无重大宏观数据发布）\n`;
  }
  report += `\n`;

  // 下周关注
  report += `### 📌 下周关注\n\n`;
  report += `- 持续关注: 中东局势演变、美伊关系\n`;
  report += `- 数据发布: 关注 PMI 等月度数据\n`;
  report += `- 政策面: 关注央行公开市场操作节奏\n\n`;

  report += `---\n*报告生成时间: ${new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}*\n`;

  return report;
}

// ========================= 月报 =========================

function generateMonthly(data) {
  const endDate = data.window.endDate;
  const startDate = data.window.startDate;
  const news = data.news || [];
  const high = news.filter(n => n.importance === 'high');

  let report = '';

  report += `## 🗓️ 宏观月报 | ${startDate} ~ ${endDate}\n\n`;
  report += `> 📡 本月采集 ${data.stats.total} 条 | 🔴 高 ${data.stats.high}\n\n`;

  // 宏观全景
  report += `### 🌏 宏观全景\n\n`;
  const macroNews = news.filter(n => n.categories.some(c => ['货币政策', '财政政策', '宏观数据', '国际财经'].includes(c)));
  for (const n of high.slice(0, 20)) {
    report += `- ${importanceEmoji(n.importance)} [${n.title}](${n.url}) — ${n.source}\n`;
  }
  report += `\n`;

  // 政策路线图
  report += `### 🛤️ 政策路线图\n\n`;
  const policyNews = news.filter(n => n.categories.some(c => ['货币政策', '财政政策', '产业政策', '监管动态'].includes(c)));
  for (const n of policyNews.filter(n => n.importance === 'high').slice(0, 10)) {
    report += `- [${n.title}](${n.url})\n`;
  }
  report += `\n`;

  // 行业景气度
  report += `### 🏭 行业轮动\n\n`;
  const sectors = Object.entries(data.stats.sectors || {}).sort((a, b) => b[1] - a[1]);
  report += `| 行业 | 新闻提及 | 关注度 |\n`;
  report += `|------|----------|--------|\n`;
  for (const [sector, count] of sectors.slice(0, 10)) {
    const level = count > 15 ? '🔥 极高' : count > 10 ? '📈 高' : count > 5 ? '👀 关注' : '➖ 正常';
    report += `| ${sector} | ${count} | ${level} |\n`;
  }
  report += `\n`;

  report += `---\n*报告生成时间: ${new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}*\n`;

  return report;
}

// ========================= 年报 =========================

function generateYearly(data) {
  const endDate = data.window.endDate;
  const startDate = data.window.startDate;
  const news = data.news || [];
  const high = news.filter(n => n.importance === 'high');

  let report = '';

  report += `## 📆 宏观年报 | ${startDate} ~ ${endDate}\n\n`;
  report += `> 📡 全年采集 ${data.stats.total} 条重大新闻\n\n`;

  // 年度回顾
  report += `### 🎯 年度回顾\n\n`;
  report += `（基于全年采集的 ${data.stats.total} 条新闻数据，以下是年度宏观环境变化的核心主线）\n\n`;

  // 重大事件时间线
  report += `### 📅 重大事件时间线\n\n`;
  // 按月分组
  const byMonth = {};
  for (const n of high) {
    const month = n.date.slice(0, 7);
    if (!byMonth[month]) byMonth[month] = [];
    if (byMonth[month].length < 5) byMonth[month].push(n);
  }

  const months = Object.keys(byMonth).sort();
  for (const month of months) {
    report += `**${month}**\n\n`;
    for (const n of byMonth[month]) {
      report += `- ${importanceEmoji(n.importance)} [${n.title}](${n.url})\n`;
    }
    report += `\n`;
  }

  // 行业年度轮动
  report += `### 🏭 行业年度全景\n\n`;
  const sectors = Object.entries(data.stats.sectors || {}).sort((a, b) => b[1] - a[1]);
  report += `| 行业 | 全年关注度 |\n`;
  report += `|------|------------|\n`;
  for (const [sector, count] of sectors.slice(0, 15)) {
    report += `| ${sector} | ${'█'.repeat(Math.max(1, Math.ceil(count / 5)))} ${count} |\n`;
  }
  report += `\n`;

  // 来年展望
  report += `### 🔮 来年展望\n\n`;
  report += `（基于年度趋势分析，以下是来年值得关注的方向和风险点）\n\n`;
  report += `- 政策延续性：关注重点产业政策的延续与调整\n`;
  report += `- 国际环境：地缘政治风险仍将是核心变量\n`;
  report += `- 技术变革：AI 等前沿科技对产业格局的重塑加速\n\n`;

  report += `---\n*报告生成时间: ${new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}*\n`;

  return report;
}

// ========================= 主流程 =========================

function loadNewsData(period, refDate) {
  // 周报/月报/年报：从日报 JSON 文件聚合
  // 新浪 Feed API 只保留最近 ~3 天数据，无法回溯历史
  if (period !== 'daily') {
    return aggregateDailyData(period, refDate);
  }

  // 日报：读取单日 JSON
  const yesterday = new Date(refDate);
  yesterday.setDate(yesterday.getDate() - 1);
  const fileKey = yesterday.toISOString().slice(0, 10);
  const file = path.join(NEWS_DIR, `${fileKey}.json`);
  if (!fs.existsSync(file)) {
    console.error(`新闻数据文件不存在: ${file}`);
    process.exit(1);
  }
  return JSON.parse(fs.readFileSync(file, 'utf-8'));
}

function aggregateDailyData(period, refDate) {
  // 从 news/ 目录中读取所有日报 JSON，按时间窗口筛选
  if (!fs.existsSync(NEWS_DIR)) {
    console.error('news 目录不存在');
    process.exit(1);
  }

  const files = fs.readdirSync(NEWS_DIR)
    .filter(f => f.endsWith('.json') && f.match(/^\d{4}-\d{2}-\d{2}\.json$/))
    .sort();

  if (files.length === 0) {
    console.error('没有日报数据文件');
    process.exit(1);
  }

  // 确定时间窗口
  let startDate, endDate, startTs, endTs;
  if (period === 'weekly') {
    const today = new Date(refDate);
    const dayOfWeek = today.getDay();
    const daysSinceMonday = dayOfWeek === 0 ? 6 : dayOfWeek - 1;
    const thisMonday = new Date(today);
    thisMonday.setDate(today.getDate() - daysSinceMonday);
    thisMonday.setHours(0, 0, 0, 0);
    endDate = thisMonday.toISOString().slice(0, 10); // 本周一（即上周末+1天标记）
    startDate = new Date(thisMonday.getTime() - 7 * 86400000).toISOString().slice(0, 10);
    startTs = Math.floor(new Date(startDate).getTime() / 1000);
    endTs = Math.floor((thisMonday.getTime() - 1000) / 1000);
  } else if (period === 'monthly') {
    const now = new Date(refDate);
    const thisMonth1st = new Date(now.getFullYear(), now.getMonth(), 1);
    endDate = thisMonth1st.toISOString().slice(0, 10);
    const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    startDate = lastMonth.toISOString().slice(0, 10);
    startTs = Math.floor(lastMonth.getTime() / 1000);
    endTs = Math.floor((thisMonth1st.getTime() - 1000) / 1000);
  } else { // yearly
    const lastYear = new Date(refDate).getFullYear() - 1;
    startDate = `${lastYear}-01-01`;
    endDate = `${lastYear}-12-31`;
    startTs = Math.floor(new Date(startDate).getTime() / 1000);
    endTs = Math.floor(new Date(`${lastYear}-12-31T23:59:59`).getTime() / 1000);
  }

  // 读取窗口内的日报文件
  const windowFiles = files.filter(f => {
    const name = f.replace('.json', '');
    return name >= startDate.slice(0, 10) && name < endDate;
  });

  console.error(`📂 聚合 ${windowFiles.length} 份日报: ${startDate} ~ ${endDate}`);

  // 合并所有日报的新闻
  const allNews = [];
  const seen = new Set();
  for (const f of windowFiles) {
    try {
      const data = JSON.parse(fs.readFileSync(path.join(NEWS_DIR, f), 'utf-8'));
      for (const n of data.news || []) {
        const key = n.id || `${n.title}|${n.url}`;
        if (!seen.has(key)) {
          seen.add(key);
          allNews.push(n);
        }
      }
    } catch (e) {
      console.error(`  读取 ${f} 失败: ${e.message}`);
    }
  }

  console.error(`📥 聚合 ${allNews.length} 条（去重后）`);

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
    for (const c of n.categories || []) {
      stats.categories[c] = (stats.categories[c] || 0) + 1;
    }
    for (const s of n.sectors || []) {
      stats.sectors[s] = (stats.sectors[s] || 0) + 1;
    }
  }

  return {
    period,
    window: { start: startTs, end: endTs, startDate, endDate },
    collected_at: new Date().toISOString(),
    stats,
    news: allNews,
    market_data: null,
  };
}

function saveReport(period, refDate, content) {
  const dir = path.join(REPORTS_DIR, PERIOD_DIRS[period]);
  let filename;

  if (period === 'daily') {
    // 日报：用昨天日期命名
    const yesterday = new Date(refDate);
    yesterday.setDate(yesterday.getDate() - 1);
    filename = yesterday.toISOString().slice(0, 10) + '.md';
  } else if (period === 'weekly') {
    // 周报：用上周的周号命名
    const thisMonday = new Date(refDate);
    const dayOfWeek = refDate.getDay();
    const daysSinceMonday = dayOfWeek === 0 ? 6 : dayOfWeek - 1;
    thisMonday.setDate(refDate.getDate() - daysSinceMonday);
    const lastSunday = new Date(thisMonday.getTime() - 86400000);
    filename = getWeekNumber(lastSunday) + '.md';
  } else if (period === 'monthly') {
    // 月报：用上月月份命名
    const d = new Date(refDate);
    const lastMonth = new Date(d.getFullYear(), d.getMonth(), 0);
    filename = lastMonth.toISOString().slice(0, 7) + '.md';
  } else {
    // 年报：用去年年份命名
    filename = (refDate.getFullYear() - 1) + '.md';
  }

  const filepath = path.join(dir, filename);
  fs.writeFileSync(filepath, content);
  console.error(`💾 报告已保存: ${filepath}`);
  return filepath;
}

function updateMarks(period, refDate) {
  let marks = {};
  if (fs.existsSync(MARKS_FILE)) {
    marks = JSON.parse(fs.readFileSync(MARKS_FILE, 'utf-8'));
  }

  // 标记记录的是"已完成报告覆盖的最新日期/周/月/年"
  if (period === 'daily') {
    // 记录"昨天"的日期，表示昨天日报已生成
    const yesterday = new Date(refDate);
    yesterday.setDate(yesterday.getDate() - 1);
    marks.daily = yesterday.toISOString().slice(0, 10);
  } else if (period === 'weekly') {
    // 记录上周的周号
    const thisMonday = new Date(refDate);
    const dow = refDate.getDay();
    const daysSinceMonday = dow === 0 ? 6 : dow - 1;
    thisMonday.setDate(refDate.getDate() - daysSinceMonday);
    const lastSunday = new Date(thisMonday.getTime() - 86400000);
    marks.weekly = getWeekNumber(lastSunday);
  } else if (period === 'monthly') {
    // 记录上月月份
    const lastMonth = new Date(refDate.getFullYear(), refDate.getMonth(), 0);
    marks.monthly = lastMonth.toISOString().slice(0, 7);
  } else if (period === 'yearly') {
    // 记录去年
    marks.yearly = String(refDate.getFullYear() - 1);
  }

  fs.writeFileSync(MARKS_FILE, JSON.stringify(marks, null, 2));
  console.error(`📌 标记已更新: ${period}`);
}

async function main() {
  const args = process.argv.slice(2);
  const period = args[0] || 'daily';
  const dateIdx = args.indexOf('--date');
  const refDate = dateIdx !== -1 ? new Date(args[dateIdx + 1]) : new Date();

  if (!['daily', 'weekly', 'monthly', 'yearly'].includes(period)) {
    console.error('用法: node report.js <daily|weekly|monthly|yearly> [--date YYYY-MM-DD]');
    process.exit(1);
  }

  console.error(`📝 生成 ${period} 报告: ${refDate.toISOString().slice(0, 10)}`);

  const data = loadNewsData(period, refDate);

  let report;
  switch (period) {
    case 'daily':   report = generateDaily(data);   break;
    case 'weekly':  report = generateWeekly(data);  break;
    case 'monthly': report = generateMonthly(data); break;
    case 'yearly':  report = generateYearly(data);  break;
  }

  // 输出到 stdout
  console.log(report);

  // 存档
  saveReport(period, refDate, report);

  // 更新标记
  updateMarks(period, refDate);
}

main().catch(err => {
  console.error('报告生成失败:', err);
  process.exit(1);
});
