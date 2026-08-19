/* ============================================================
 * dino.js — 页眉恐龙跳一跳（Chrome 断网小恐龙的迷你版）
 * ------------------------------------------------------------
 * 在 Portal 顶栏 .portal-bar 中间塞一个小 canvas，跑一个极简
 * runner：恐龙在地面上跑，仙人掌从右往左来，空格/↑/点击起跳，
 * 撞到就 GAME OVER，可点击重开。最高分存 localStorage。
 *
 * 设计约束（为什么这么小）：
 *   - 顶栏高约 45px，下面的 .app-tabs-bar 是写死的 sticky top:47px，
 *     iframe 高度是 calc(100vh - 48px)。所以游戏 canvas 用负外边距
 *     抵消 flex 撑高，绝不改变顶栏高度，零布局影响、易回退。
 *
 * 交互护栏：
 *   - 焦点在 input/textarea/select/可编辑元素时，空格不劫持（不打断打字）。
 *   - 只有真正处理了按键才 preventDefault，避免页面被空格滚动。
 *   - 页面隐藏时停 rAF；恐龙彩蛋纯前端，不碰后端、不影响统计。
 *
 * 回退：删 index.html 里 <script src="/dino.js"> 一行即可，零副作用。
 * ============================================================ */
(function () {
  'use strict';

  // ---- 画布尺寸（CSS px）。刻意做矮，塞进顶栏不撑高 ----
  var W = 320, H = 30;        // 加长跑道：更宽 = 障碍来得更从容
  var GROUND_Y = 25;          // 地面基线：恐龙脚底 / 仙人掌底
  var DINO_X = 16;
  var DINO_W = 12, DINO_H = 13;
  // 重力越小滞空越久：滞空帧数 = 2×|JUMP_V|÷GRAVITY。
  // 从 ~17 帧拉到 ~29 帧，峰高仍约 16px，跳跃更「飘」、时机好判定。
  var GRAVITY = 0.16;
  var JUMP_V = -2.3;          // 峰值升起约 16px，够跨过仙人掌
  var SPEED0 = 1.5, SPEED_INC = 0.0007, SPEED_MAX = 3.0;  // 起步更慢、加速更缓、封顶更低
  var HI_KEY = 'portal_dino_best';

  function init() {
    var bar = document.querySelector('.portal-bar');
    if (!bar || document.getElementById('_dinoCanvas')) return;

    var wrap = document.createElement('span');
    wrap.id = '_dinoWrap';
    wrap.title = '断网小恐龙 · 空格/点击起跳';
    // 负外边距：让 canvas 的外框高度 ≈ 顶栏原有行高，不把顶栏撑高
    wrap.style.cssText =
      'display:flex;align-items:center;flex:0 0 auto;margin:-6px 8px;cursor:pointer;user-select:none;';

    var canvas = document.createElement('canvas');
    canvas.id = '_dinoCanvas';
    var dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.cssText = 'width:' + W + 'px;height:' + H + 'px;display:block;';
    wrap.appendChild(canvas);

    // 放到「左侧信息」和「右侧状态/退出」之间
    bar.insertBefore(wrap, bar.lastElementChild);

    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // ---- 主题色（跟随 CSS 变量，明暗都能看清）----
    function cssVar(name, fallback) {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name);
      v = (v || '').trim();
      return v || fallback;
    }
    var COL_INK, COL_GROUND, COL_MUTED;
    function refreshColors() {
      COL_INK = cssVar('--text', '#17191f');
      COL_GROUND = cssVar('--border', '#94a3b8');
      COL_MUTED = cssVar('--muted', '#94a3b8');
    }
    refreshColors();

    // ---- 状态 ----
    var STATE = { IDLE: 0, PLAY: 1, DEAD: 2 };
    var state = STATE.IDLE;
    var dinoY = GROUND_Y - DINO_H;   // 恐龙 top
    var vy = 0;
    var speed = SPEED0;
    var obstacles = [];
    var spawnIn = 40;
    var frame = 0;
    var score = 0;
    var best = parseInt(localStorage.getItem(HI_KEY) || '0', 10) || 0;
    var groundShift = 0;
    var rafId = 0;

    function reset() {
      dinoY = GROUND_Y - DINO_H;
      vy = 0;
      speed = SPEED0;
      obstacles = [];
      spawnIn = 40;
      frame = 0;
      score = 0;
      groundShift = 0;
    }

    function grounded() { return dinoY >= GROUND_Y - DINO_H - 0.01; }

    function jump() {
      if (grounded()) vy = JUMP_V;
    }

    function start() {
      reset();
      state = STATE.PLAY;
      loop();
    }

    function spawn() {
      var h = 6 + Math.floor(Math.random() * 3);   // 6~8，略矮，更好跨
      var w = 5 + Math.floor(Math.random() * 4);    // 5~8
      obstacles.push({ x: W + 2, w: w, h: h });
      // 速度降了，间隔帧数要同步拉大，否则像素间距反而更密
      spawnIn = 95 + Math.floor(Math.random() * 75); // 帧倒计时
    }

    function step() {
      frame++;
      score = Math.floor(frame / 5);
      speed = Math.min(SPEED_MAX, speed + SPEED_INC);
      groundShift = (groundShift + speed) % 12;

      // 物理
      vy += GRAVITY;
      dinoY += vy;
      if (dinoY > GROUND_Y - DINO_H) { dinoY = GROUND_Y - DINO_H; vy = 0; }

      // 障碍
      if (--spawnIn <= 0) spawn();
      for (var i = obstacles.length - 1; i >= 0; i--) {
        obstacles[i].x -= speed;
        if (obstacles[i].x + obstacles[i].w < -2) obstacles.splice(i, 1);
      }

      // 碰撞（各方向收 2px，判定更宽容）
      var dx = DINO_X + 2, dw = DINO_W - 4;
      var dy = dinoY + 2, dh = DINO_H - 4;
      for (var j = 0; j < obstacles.length; j++) {
        var o = obstacles[j];
        var ox = o.x + 1, ow = o.w - 2;
        var oy = GROUND_Y - o.h + 1, oh = o.h - 2;
        if (dx < ox + ow && dx + dw > ox && dy < oy + oh && dy + dh > oy) {
          gameOver();
          return;
        }
      }
    }

    function gameOver() {
      state = STATE.DEAD;
      if (score > best) { best = score; localStorage.setItem(HI_KEY, String(best)); }
      draw();
    }

    // ---- 绘制 ----
    function drawDino() {
      ctx.fillStyle = COL_INK;
      var x = DINO_X, top = Math.round(dinoY);
      // 头
      ctx.fillRect(x + 6, top, 6, 5);
      // 身体
      ctx.fillRect(x + 2, top + 4, 8, 5);
      // 尾巴
      ctx.fillRect(x, top + 5, 2, 3);
      // 腿：跑步时交替；跳跃/死亡时并拢
      var run = (state === STATE.PLAY) && grounded() && (Math.floor(frame / 5) % 2 === 0);
      if (state === STATE.PLAY && grounded()) {
        if (run) {
          ctx.fillRect(x + 3, top + 9, 2, 4);
          ctx.fillRect(x + 8, top + 9, 2, 3);
        } else {
          ctx.fillRect(x + 3, top + 9, 2, 3);
          ctx.fillRect(x + 8, top + 9, 2, 4);
        }
      } else {
        ctx.fillRect(x + 3, top + 9, 2, 4);
        ctx.fillRect(x + 8, top + 9, 2, 4);
      }
      // 眼睛（挖一个背景色点）
      ctx.clearRect(x + 9, top + 1, 1, 1);
    }

    function drawGround() {
      ctx.strokeStyle = COL_GROUND;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, GROUND_Y + 0.5);
      ctx.lineTo(W, GROUND_Y + 0.5);
      ctx.stroke();
      // 移动的小碎石，制造速度感
      ctx.fillStyle = COL_GROUND;
      for (var gx = -groundShift; gx < W; gx += 12) {
        ctx.fillRect(Math.round(gx), GROUND_Y + 3, 3, 1);
      }
    }

    function drawObstacles() {
      ctx.fillStyle = COL_INK;
      for (var i = 0; i < obstacles.length; i++) {
        var o = obstacles[i];
        ctx.fillRect(Math.round(o.x), GROUND_Y - o.h, o.w, o.h);
        // 仙人掌的小臂
        ctx.fillRect(Math.round(o.x) - 2, GROUND_Y - o.h + 3, 2, 2);
        ctx.fillRect(Math.round(o.x) + o.w, GROUND_Y - o.h + 2, 2, 2);
      }
    }

    function drawScore() {
      ctx.fillStyle = COL_MUTED;
      ctx.font = '10px ui-monospace,Menlo,Consolas,monospace';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'top';
      var hi = best ? 'HI ' + String(best).padStart(4, '0') + '  ' : '';
      ctx.fillText(hi + String(score).padStart(4, '0'), W, 0);
      ctx.textAlign = 'left';
    }

    function drawHint(text) {
      ctx.fillStyle = COL_MUTED;
      ctx.font = '10px ui-sans-serif,system-ui,sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, W / 2, H / 2 - 1);
      ctx.textAlign = 'left';
    }

    function draw() {
      ctx.clearRect(0, 0, W, H);
      drawGround();
      drawObstacles();
      drawDino();
      drawScore();
      if (state === STATE.IDLE) drawHint('点击 / 空格 开始');
      else if (state === STATE.DEAD) drawHint('GAME OVER · 点击重开');
    }

    function loop() {
      if (state !== STATE.PLAY) return;
      step();
      if (state === STATE.PLAY) {
        draw();
        rafId = requestAnimationFrame(loop);
      }
    }

    // ---- 输入 ----
    function onAction() {
      if (state === STATE.PLAY) jump();
      else start();
    }

    canvas.addEventListener('click', function (e) {
      e.preventDefault();
      onAction();
    });

    function isTyping() {
      var el = document.activeElement;
      if (!el) return false;
      var tag = el.tagName;
      return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
    }

    window.addEventListener('keydown', function (e) {
      if (e.code !== 'Space' && e.key !== ' ' && e.code !== 'ArrowUp' && e.key !== 'ArrowUp') return;
      if (isTyping()) return;          // 别打断打字
      e.preventDefault();              // 阻止空格滚动页面
      onAction();
    });

    document.addEventListener('visibilitychange', function () {
      if (document.hidden && rafId) { cancelAnimationFrame(rafId); rafId = 0; }
      else if (!document.hidden && state === STATE.PLAY && !rafId) { loop(); }
    });

    // 主题切换或字体变化时刷新配色再重绘
    window.addEventListener('resize', function () { refreshColors(); if (state !== STATE.PLAY) draw(); });

    draw(); // 初始待机帧
  }

  if (document.querySelector('.portal-bar')) init();
  else document.addEventListener('DOMContentLoaded', init);
})();
