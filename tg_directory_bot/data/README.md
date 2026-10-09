# 数据文件说明

## life_guide.json（📖 人生指南）

- 来源：《高性价比人生指南》 https://github.com/eternity4719/HowToLiveBetter
- 许可：正文 CC BY 4.0（https://creativecommons.org/licenses/by/4.0/）
- 改动：只节选了每一条的标题、「成本」「说人话」「证据等级」和每节开头的介绍，并重新排版；未包含收益、来源、备注等其它内容。
- 同步版本：见 JSON 里的 `version` 字段（提交号 + 日期）。
- 更新方法：

```bash
git clone --depth 1 https://github.com/eternity4719/HowToLiveBetter /tmp/HowToLiveBetter
python scripts/build_life_guide.py /tmp/HowToLiveBetter
```
