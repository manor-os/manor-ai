// One sample of each state of the read-only reply cards, shaped as render_response_surface stores them.
export const RESPONSE_CARD_SAMPLES: Array<{ name: string; template: string; say: string; props: Record<string, unknown> }> = [
  { name: "邮件 · 收到一封", template: "card.email", say: "星河餐饮回信了，想把试饮改到周五。", props: {
    status: "received", from_name: "陈经理", from_address: "chen@xinghe.example", subject: "回复：试饮安排", sent_at: "09:41",
    snippet: "周四临时有活动，能不能改到周五下午？人数不变，还是 30 位……",
    body: "周四临时有活动，能不能改到周五下午？人数不变，还是 30 位。场地我们这边可以协调，流程表附在后面，麻烦看一下。",
    attachments: [{ name: "试饮流程.pdf", url: "/viewer/01M5KEWXCEFY6PT79TGQSCHVM5" }],
  } },
  { name: "邮件 · 草稿", template: "card.email", say: "回信写好了，从你的邮箱发出。", props: {
    status: "draft", to: ["陈经理"], subject: "回复：试饮安排", body: "陈经理您好，周五下午 2 点可以。流程表我们看过了，没有问题，当天见。",
  } },
  { name: "发送结果", template: "card.delivery", say: "40 位收到了，3 位没发出去。", props: {
    items: [
      { label: "邮件", status: "sent", count: 40, url: "https://example.com/sent" },
      { label: "TikTok", status: "published", url: "https://example.com/post" },
      { label: "小红书", status: "scheduled", detail: "今晚 20:00" },
      { label: "邮件被退回", status: "failed", count: 3, detail: "地址不存在" },
    ],
  } },
  { name: "搜索结果", template: "card.search_results", say: "找到 12 条，这 3 条最相关。", props: {
    total: 12,
    results: [
      { title: "2026 精品咖啡消费报告", source: "coffee-report.example", snippet: "家用器具增长最快，磨豆机领先", url: "https://coffee-report.example/2026" },
      { title: "10 月新品试饮复盘", source: "知识库", when: "上周更新", url: "/viewer/01M5KEWXCEFY6PT79TGQSCHVM8" },
      { title: "回复：试饮安排", source: "邮箱 · 陈经理", when: "今天" },
    ],
  } },
  { name: "帖子 · 各平台", template: "card.post", say: "同一条内容，改成了三个平台各自的版本。", props: {
    posts: [
      { platform: "TikTok", account: "@beanpop", format: "vertical_video", status: "draft", text: "30 秒看完这台磨豆机值不值得买 #咖啡 #开箱", media: { name: "开箱预告.mp4", url: "/viewer/01M5KEWXCEFY6PT79TGQSCHVM1" } },
      { platform: "小红书", account: "Bean Pop", format: "portrait_image", status: "scheduled", title: "新手买磨豆机，先看这 4 点", text: "踩过坑才知道，刀盘比品牌更重要……", media: { name: "封面.png", url: "/viewer/01M5KEWXCEFY6PT79TGQSCHVM1" } },
      { platform: "X", account: "@beanpop", format: "text", status: "failed", text: "磨豆机比咖啡机更值得先花钱。", status_detail: "账号未连接" },
    ],
  } },
  { name: "帖子 · 多图图文", template: "card.post", say: "小红书图文写好了，6 张图都在。", props: {
    posts: [{ platform: "小红书", account: "Bean Pop", format: "portrait_image", status: "draft", title: "新手买磨豆机，先看这 4 点", text: "踩过坑才知道，刀盘比品牌更重要…… #磨豆机 #手冲入门",
      images: Array.from({ length: 6 }, (_, index) => ({ name: `第 ${index + 1} 张.png`, url: "/viewer/01M5KEWXCEFY6PT79TGQSCHVM1" })) }],
  } },
  { name: "帖子 · 图片轮播", template: "card.post", say: "Instagram 版用了 3 张方图。", props: {
    posts: [{ platform: "Instagram", account: "@beanpop.coffee", format: "square_image", status: "scheduled", text: "新手买磨豆机，先看这 4 点。",
      images: Array.from({ length: 3 }, (_, index) => ({ name: `第 ${index + 1} 张.png`, url: "/viewer/01M5KEWXCEFY6PT79TGQSCHVM1" })) }],
  } },
  { name: "帖子 · 其他平台", template: "card.post", say: "YouTube 和 LinkedIn 的版本也好了。", props: {
    posts: [
      { platform: "YouTube", account: "Bean Pop 咖啡", format: "horizontal_video", status: "draft", title: "三台磨豆机盲测：差别比想象的大", text: "同一款豆子，三台机器，五位评委。", media: { name: "盲测.mp4", url: "/viewer/01M5KEWXCEFY6PT79TGQSCHVM1" } },
      { platform: "LinkedIn", account: "Bean Pop 咖啡", format: "text", status: "draft", text: "我们用三个月把门店复购率提高了 12%。方法并不复杂：先把磨豆这一步做稳定。" },
      { platform: "未知平台", format: "text", status: "draft", text: "没有 logo 的平台只写名字。" },
    ],
  } },
  { name: "评论 · 收到的", template: "card.comment", say: "昨晚那条有 86 条评论，这 3 条最值得回。", props: {
    platform: "TikTok", account: "@beanpop", post_title: "30 秒看完这台磨豆机值不值得买", post_url: "https://example.com/post", total: 86,
    comments: [
      { author: "@lumi.daily", text: "这台平刀磨浅烘真的够细吗？想买但有点犹豫。", when: "2 小时前", likes: 124, url: "https://example.com/c/1" },
      { author: "@brewlab", text: "价格比上个月涨了，还值得吗？", when: "3 小时前", likes: 38 },
      { author: "@morning", text: "清洁麻烦吗？", when: "昨天" },
    ],
  } },
  { name: "评论 · 回复草稿和已回复", template: "card.comment", say: "回了 1 条，另 1 条写好了等你看。", props: {
    platform: "小红书", account: "Bean Pop", post_title: "新手买磨豆机，先看这 4 点",
    comments: [
      { author: "咖啡小白", text: "预算 500 能买什么？", when: "今天", reply: { text: "500 左右可以先看手摇，磨细一致性比电动入门款更好。", status: "draft" } },
      { author: "阿杰", text: "写得很清楚，感谢！", when: "昨天", likes: 12, reply: { author: "Bean Pop", text: "谢谢支持，有问题随时问。", status: "sent" } },
    ],
  } },
  { name: "评论 · 我们发出的", template: "card.comment", say: "在这位博主的帖子下留了言，另一条被平台拦了。", props: {
    platform: "X", post_title: "Which grinder should a beginner buy?",
    comments: [
      { author: "@beanpop", text: "Start with how you brew: pour-over and espresso need different things.", status: "sent", when: "刚刚" },
      { author: "@beanpop", text: "We ran a blind test on this, happy to share.", status: "failed" },
    ],
  } },
  { name: "帖子 · 长草稿", template: "card.post", say: "小红书版写得比较长，先看开头。", props: {
    posts: [{ platform: "小红书", format: "text", status: "draft", text: Array.from({ length: 16 }, (_, index) => `第 ${index + 1} 段：刀盘比品牌更重要。`).join("\n") }],
  } },
  { name: "帖子 · 已发布带数据", template: "card.post", say: "昨晚这条表现不错，互动率是平时的两倍。", props: {
    posts: [{ platform: "TikTok", account: "@beanpop", format: "text", status: "published", url: "https://example.com/post", metrics: [{ label: "播放", value: "18.9K" }, { label: "赞", value: "1,240" }, { label: "评论", value: "86" }] }],
  } },
  { name: "名片", template: "card.person", say: "这位是这次试饮的对接人。", props: {
    name: "陈经理", organization: "星河餐饮", title: "市场经理", email: "chen@xinghe.example", note: "最近联系：今天，试饮改期",
  } },
  { name: "日程", template: "card.event", say: "改到周五下午，另一场取消了。", props: {
    events: [
      { title: "新品试饮", date: "2026-11-13", time: "14:00–16:00", location: "静安 Loft", attendees: ["林", "周", "陈经理"], status: "proposed" },
      { title: "原定试饮", date: "2026-11-12", time: "14:00", status: "cancelled" },
    ],
  } },
  { name: "股票", template: "card.stock", say: "今天小幅上涨，盘中没有大波动。", props: {
    name: "示例股份", symbol: "DEMO", price: 128.4, currency: "$", change: 1.52, change_percent: 1.2, as_of: "延迟 15 分钟", history: [126.9, 126.5, 127.2, 127, 127.8, 127.5, 128.1, 128.4],
  } },
  { name: "商品", template: "card.product", say: "这两台性价比最高。", props: {
    products: [
      { name: "M2 平刀磨豆机", price: "¥1,299", seller: "示例商城", detail: "库存 42 台", image: { name: "m2.png", url: "/viewer/01M5KEWXCEFY6PT79TGQSCHVM1" }, url: "https://example.com/m2" },
      { name: "C1 手摇磨豆机", price: "¥499", seller: "示例商城" },
    ],
  } },
  { name: "地图", template: "card.map", say: "附近有 3 家能做试饮场地。", props: {
    places: [
      { name: "静安 Loft", address: "南京西路", detail: "1.2 km", lat: 31.229, lng: 121.449 },
      { name: "南京西路共享空间", detail: "2.4 km", lat: 31.232, lng: 121.463 },
      { name: "新天地活动厅", detail: "3.1 km", lat: 31.219, lng: 121.475 },
      { name: "还没查到位置的场地" },
    ],
  } },
  { name: "图表 · 趋势", template: "card.chart", say: "互动率连续 3 周上升，这周超过目标。", props: {
    chart_type: "line", unit: "%", categories: ["41 周", "42 周", "43 周", "44 周"], series: [{ name: "互动率", values: [3.1, 3.4, 3.8, 4.1] }], reference: { label: "目标 4%", value: 4 },
  } },
  { name: "图表 · 两组对比", template: "card.chart", say: "TikTok 一路上升，小红书基本持平。", props: {
    chart_type: "line", unit: "%", categories: ["41 周", "42 周", "43 周", "44 周"], series: [{ name: "TikTok", values: [3.2, 3.9, 4.6, 5.2] }, { name: "小红书", values: [2.8, 2.9, 2.6, 2.7] }],
  } },
  { name: "图表 · 数量", template: "card.chart", say: "这周发布了 12 条，是 6 周以来最多的一周。", props: {
    chart_type: "bar", categories: ["39", "40", "41", "42", "43", "44"], series: [{ name: "发布", values: [5, 7, 6, 9, 8, 12] }],
  } },
  { name: "图表 · 占比", template: "card.chart", say: "一半预算花在 TikTok。", props: {
    chart_type: "share", unit: "¥", categories: ["TikTok", "小红书", "新创作者"], series: [{ name: "预算", values: [1500, 1000, 500] }],
  } },
  { name: "图表 · 排行", template: "card.chart", say: "互动最高的 3 位创作者。", props: {
    chart_type: "ranking", unit: "%", categories: ["@lumi.daily", "@brewlab", "@morning"], series: [{ name: "互动率", values: [6.3, 5.2, 3.9] }],
  } },
];
