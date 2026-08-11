#!/usr/bin/env node
/**
 * 一键执行：采集 + 报告 + 更新标记
 * 用法: node run.js <daily|weekly|monthly|yearly> [--date YYYY-MM-DD]
 */

const { execFileSync } = require('child_process');
const path = require('path');

const SCRIPTS_DIR = __dirname;

function run(period, refDate) {
  console.error(`\n🚀 执行 ${period} 报告流程\n`);
  const extraArgs = refDate ? ['--date', refDate] : [];

  console.error('━━━ Step 1/2: 采集新闻 ━━━');
  try {
    execFileSync(process.execPath, [path.join(SCRIPTS_DIR, 'collect.js'), period, ...extraArgs], {
      stdio: 'inherit',
      timeout: 120000,
      env: process.env,
    });
  } catch (err) {
    console.error('采集失败:', err.message);
    process.exit(1);
  }

  console.error('\n━━━ Step 2/2: 生成报告 ━━━');
  try {
    const output = execFileSync(process.execPath, [path.join(SCRIPTS_DIR, 'report.js'), period, ...extraArgs], {
      encoding: 'utf-8',
      timeout: 30000,
      env: process.env,
    });
    console.log(output);
  } catch (err) {
    console.error('报告生成失败:', err.message);
    process.exit(1);
  }

  console.error('\n✅ 完成');
}

const args = process.argv.slice(2);
const period = args[0];
const dateIndex = args.indexOf('--date');
const refDate = dateIndex >= 0 ? args[dateIndex + 1] : null;
if (!period || !['daily', 'weekly', 'monthly', 'yearly'].includes(period)) {
  console.error('用法: node run.js <daily|weekly|monthly|yearly> [--date YYYY-MM-DD]');
  process.exit(1);
}
if (refDate && !/^\d{4}-\d{2}-\d{2}$/.test(refDate)) {
  console.error('日期格式必须为 YYYY-MM-DD');
  process.exit(1);
}

run(period, refDate);
