/* lib.js —— 纯展示工具（格式化 / 样式类名 / SVG 图表 / 纯渲染函数）
 *
 * 【是什么】从 index.html 的 <script> 里搬出来的纯函数：给同样的输入就给同样的输出，
 *   不读页面状态（S / filters / LOCAL）、不碰 DOM、不发请求、不绑事件。逐字复制，调用点没改。
 *
 * 【最重要的规矩】页面那个 <script> 是 IIFE，里面的函数**不是全局的**，
 *   所以 lib.js 里的函数**绝不能回调页面函数** —— 会 undefined，被 load() 的 catch 吞成「离线」，
 *   整页提示连不上后台（2026-09-22 真的这么炸过一次，列表和看板两个视图直接白掉）。
 *   凡是需要回调页面的函数，就留在页面里别搬。tools/test_web_lib.js 里有这条检查。
 *
 * 【怎么加载】index.html 里 <script src="lib.js"></script> 放在页面脚本之前；
 *   服务端有 /lib.js 路由，写相对路径是为了 file:// 直接打开（分享版）也能用。
 */
  function esc(s){
    return String(s == null ? "" : s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
  }


  function today(){
    var d = new Date();
    return d.getFullYear() + "-" + pad(d.getMonth()+1) + "-" + pad(d.getDate());
  }


  function pad(n){ return n < 10 ? "0"+n : ""+n; }


  function fmtDate(s){
    if(!s) return "";
    return String(s).replace(/^(\d{4})-(\d{2})-(\d{2}).*$/, "$1-$2-$3");
  }


  function fmtStamp(s){
    if(!s) return "—";
    return String(s).slice(0,16);
  }


  function safeUrl(u){
    u = String(u||"").trim();
    if(/^https?:\/\//i.test(u)) return u;
    return "";
  }


  function ymd(date){
    return date.getFullYear() + "-" + pad(date.getMonth()+1) + "-" + pad(date.getDate());
  }


  function companyGroups(apps){
    var map = {}, order = [];
    apps.forEach(function(a){
      var key = (a.company || "未分类").trim();
      if(!map[key]){ map[key] = []; order.push(key); }
      map[key].push(a);
    });
    return order.map(function(key){ return {company:key, items:map[key]}; });
  }


  function statusRankOf(status){
    if(status === "Offer") return 0;
    if(status === "进行中") return 1;
    if(status === "已拒绝") return 2;
    return 3;
  }


  function bestStatusOf(items){
    var best = 3, label = "未登记";
    items.forEach(function(a){
      var r = statusRankOf(a.derived_status);
      if(r < best){ best = r; label = a.derived_status; }
    });
    return label;
  }


  function leadItemOf(items){
    var best = null, bestRank = 99;
    items.forEach(function(a){
      var r = statusRankOf(a.derived_status);
      if(r < bestRank || (r === bestRank && best && (a.last_event_at || "") > (best.last_event_at || ""))){
        best = a;
        bestRank = r;
      }
    });
    return best || items[0];
  }


  function dashCard(cls, title, sub, body){
    return '<div class="dash-card ' + cls + '"><h3>' + esc(title) + '</h3><div class="sub">' + esc(sub) + '</div>' + body + '</div>';
  }


  function lineChartSvg(series){
    var w = 560, h = 180, padL = 34, padR = 14, padT = 14, padB = 26;
    var innerW = w - padL - padR, innerH = h - padT - padB;
    var max = 1;
    series.forEach(function(d){ if(d.value > max) max = d.value; });
    var n = series.length;
    var stepX = n > 1 ? innerW / (n - 1) : 0;
    var pts = series.map(function(d, i){
      var x = padL + i * stepX;
      var y = padT + innerH - (d.value / max) * innerH;
      return {x:x, y:y, d:d};
    });
    var line = pts.map(function(p, i){ return (i ? "L" : "M") + p.x.toFixed(1) + " " + p.y.toFixed(1); }).join(" ");
    var area = line + " L" + (padL + innerW).toFixed(1) + " " + (padT + innerH) + " L" + padL + " " + (padT + innerH) + " Z";
    var gid = "lg" + (++chartSeq);
    var grid = "";
    for(var g = 0; g <= 2; g++){
      var gy = padT + innerH - (g / 2) * innerH;
      grid += '<line x1="' + padL + '" y1="' + gy.toFixed(1) + '" x2="' + (padL + innerW) + '" y2="' + gy.toFixed(1) + '" stroke="var(--line)" stroke-width="1"/>';
    }
    var labels = "";
    pts.forEach(function(p, i){
      if(i % 5 === 0 || i === n - 1){
        labels += '<text x="' + p.x.toFixed(1) + '" y="' + (h - 8) + '" text-anchor="middle" font-size="10" fill="var(--muted)">' + esc(p.d.label.slice(5)) + '</text>';
      }
    });
    var dots = pts.map(function(p){
      if(!p.d.value) return "";
      return '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="2.6" fill="var(--accent)"><title>' + esc(p.d.label) + '：' + p.d.value + ' 条</title></circle>';
    }).join("");
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" class="chart-line">'
      + '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">'
      + '<stop offset="0%" stop-color="var(--accent)" stop-opacity=".28"/>'
      + '<stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>'
      + grid
      + '<path d="' + area + '" fill="url(#' + gid + ')"/>'
      + '<path d="' + line + '" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
      + dots + labels + '</svg>';
  }


  function donutSvg(buckets, total, centerLabel){
    var size = 170, r = 62, cx = 85, cy = 85, stroke = 20;
    var segs = [
      {label:"Offer", value:buckets.offer, color:"var(--green)"},
      {label:"投递进程中", value:buckets.active, color:"var(--gray)"},
      {label:"已拒绝", value:buckets.reject, color:"var(--red)"},
      {label:"已放弃 / 未登记", value:buckets.drop, color:"#c3c9d6"}
    ];
    var circ = 2 * Math.PI * r;
    var offset = 0;
    var arcs = segs.map(function(s){
      if(!s.value) return "";
      var frac = total ? s.value / total : 0;
      var dash = frac * circ;
      var el = '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + s.color + '" stroke-width="' + stroke
        + '" stroke-dasharray="' + dash.toFixed(2) + ' ' + (circ - dash).toFixed(2)
        + '" stroke-dashoffset="' + (-offset).toFixed(2) + '" transform="rotate(-90 ' + cx + ' ' + cy + ')">'
        + '<title>' + esc(s.label) + '：' + s.value + '</title></circle>';
      offset += dash;
      return el;
    }).join("");
    var legend = segs.map(function(s){
      return '<span class="lg"><i style="background:' + s.color + '"></i>' + esc(s.label) + ' <b>' + s.value + '</b></span>';
    }).join("");
    return '<div class="donut-wrap"><svg viewBox="0 0 ' + size + ' ' + size + '" class="chart-donut">'
      + '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="var(--panel-2)" stroke-width="' + stroke + '"/>'
      + arcs
      + '<text x="' + cx + '" y="' + (cy - 2) + '" text-anchor="middle" font-size="24" font-weight="800" fill="var(--text)">' + total + '</text>'
      + '<text x="' + cx + '" y="' + (cy + 18) + '" text-anchor="middle" font-size="11" fill="var(--muted)">' + esc(centerLabel || "总投递") + '</text>'
      + '</svg><div class="donut-legend">' + legend + '</div></div>';
  }


  function barsHtml(data){
    if(!data.length) return '<div class="chart-empty">暂无数据</div>';
    var max = 1;
    data.forEach(function(d){ if(d.value > max) max = d.value; });
    return '<div class="hbars">' + data.map(function(d){
      var pct = Math.round(d.value / max * 100);
      return '<div class="hbar"><span class="hbar-label" title="' + esc(d.label) + '">' + esc(d.label) + '</span>'
        + '<div class="hbar-track"><div class="hbar-fill" style="width:' + pct + '%"></div></div>'
        + '<span class="hbar-val">' + d.value + '</span></div>';
    }).join("") + '</div>';
  }


  function columnsHtml(data){
    if(!data.length) return '<div class="chart-empty">暂无数据</div>';
    var max = 1;
    data.forEach(function(d){ if(d.value > max) max = d.value; });
    return '<div class="cols">' + data.map(function(d){
      var pct = Math.max(10, Math.round(d.value / max * 100));
      return '<div class="col"><span class="col-val">' + d.value + '</span>'
        + '<div class="col-bar" style="height:' + pct + '%"></div>'
        + '<span class="col-lab">' + esc(d.label.slice(5)) + '月</span></div>';
    }).join("") + '</div>';
  }


  function daysSince(dateStr){
    var raw = (dateStr || "").slice(0, 10);
    if(!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return null;
    var then = new Date(raw + "T00:00:00");
    if(isNaN(then.getTime())) return null;
    var now = new Date();
    now.setHours(0, 0, 0, 0);
    return Math.round((now - then) / 86400000);
  }


  function ageText(days){
    if(days === null) return "";
    if(days <= 0) return "今天登记";
    if(days === 1) return "昨天登记";
    return "已 " + days + " 天";
  }


  function ageClass(days){
    if(days === null) return "";
    if(days >= 7) return " stale";
    if(days >= 3) return " aging";
    return "";
  }


  function tintClass(status){
    if(status === "Offer") return " tint-offer";
    if(status === "已拒绝") return " tint-reject";
    if(status === "已放弃" || status === "未登记") return " tint-drop";
    return " tint-active";
  }


  function statusClass(status){
    if(status === "Offer") return "offer";
    if(status === "已拒绝") return "reject";
    if(status === "已放弃") return "drop";
    if(status === "未登记") return "none";
    return "";
  }


  function resultClass(result){
    if(result === "通过") return "ok";
    if(result === "未通过") return "no";
    if(result === "待定") return "wait";
    if(result === "已放弃") return "drop";
    return "";
  }


  function dotClass(result){
    if(result === "通过") return "ok";
    if(result === "未通过") return "no";
    if(result === "待定") return "wait";
    if(result === "已放弃") return "drop";
    return "";
  }


  // ---------------- 宣讲会视图 ----------------
  // 一行 = 一家公司的一场宣讲会；按日期分组、三列网格展示。
  // 点公司名展开口碑摘要 / 现场必问 / 备注。
  function weekdayLabel(dateStr){
    var text = String(dateStr || "").trim();
    var parts = text.split("-");
    if(parts.length !== 3) return text || "未填日期";
    var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    if(isNaN(d.getTime())) return text;
    var names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
    return text + " " + names[d.getDay()];
  }


  function talkStatusClass(status){
    if(status === "已参加" || status === "已投递" || status === "已面试") return "st-done";
    if(status === "已放弃" || status === "已过期" || status === "已取消") return "st-drop";
    return "st-todo";
  }


  function ratingClass(rating){
    var text = String(rating || "");
    if(text.indexOf("✅") >= 0) return "rate-good";
    if(text.indexOf("❌") >= 0) return "rate-bad";
    if(text.indexOf("⚠") >= 0) return "rate-mid";
    return "st-todo";
  }


  // 口碑排序权重：✅ 最优，其次 ⚠️，再次 ❌，最后是没调研过的
  function talkRatingRank(rating){
    var text = String(rating || "");
    if(!text) return 3;
    if(text.indexOf("✅") >= 0) return 0;
    if(text.indexOf("⚠") >= 0) return 1;
    if(text.indexOf("❌") >= 0) return 2;
    return 3;
  }


  // 对口排序权重：强对口 > 一般对口 > 不对口 > 未判定
  function talkFitRank(level){
    if(level === "strong") return 0;
    if(level === "mid") return 1;
    if(level === "low") return 2;
    return 3;
  }


  function fitShort(level){
    if(level === "strong") return "🟢 强对口";
    if(level === "mid") return "🟡 一般对口";
    if(level === "low") return "⚪ 不对口";
    return "⚪ 未判定";
  }


  function renderTalkCard(t, showDate, rank){
    // 精简卡片：只留公司名 + 状态/口碑/对口 + 时间地点，点开看详情弹窗
    var status = t.status || "待参加";
    var fitLevel = t.fit_level || "";
    var bits = [];
    if(showDate) bits.push(fmtDate(t.talk_date));
    if(t.time_slot) bits.push(t.time_slot);
    if(t.location) bits.push(t.location);
    return '<article class="talk-card compact ' + talkStatusClass(status) + '"'
      + ' data-act="talk-open" data-id="' + t.id + '" title="点开查看完整详情">'
      + '<div class="talk-fit-bar fit-' + (fitLevel || "low") + '"></div>'
      + '<div class="talk-c-head">'
      +   (rank ? '<span class="talk-rank' + (rank <= 3 ? " top" : "") + '">#' + rank + '</span>' : '')
      +   '<span class="talk-c-name">' + esc(t.company) + '</span>'
      + '</div>'
      + '<div class="talk-c-tags">'
      +   '<span class="badge ' + talkStatusClass(status) + '">' + esc(status) + '</span>'
      +   (t.rating
            ? '<span class="badge ' + ratingClass(t.rating) + '">' + esc(t.rating) + '</span>'
            : '<span class="badge st-todo">未调研</span>')
      +   '<span class="badge fit-' + (fitLevel || "low") + '">' + esc(fitShort(fitLevel)) + '</span>'
      + '</div>'
      + '<div class="talk-c-meta">' + esc(bits.filter(Boolean).join(" · ") || "时间地点待补充") + '</div>'
      + '</article>';
  }


  function actionLabel(action){
    var map = {
      create:"新增", update:"修改", "delete":"删除",
      split:"拆分", restore:"恢复", auto_complete:"自动完成"
    };
    return map[action] || action;
  }


  function actionClass(action){
    if(action === "create" || action === "restore") return "create";
    if(action === "delete") return "delete";
    if(action === "auto_complete") return "auto_complete";
    return "";
  }


  var chartSeq = 0;
