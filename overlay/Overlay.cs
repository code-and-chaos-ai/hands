// Hands overlay: the agent's presence on the person's screen while it works.
// Deep-pink edge around the window he's in, a pill to stop him, his own cursor
// (never the person's), and the consent card. C# 5 for the Framework compiler. Apache-2.0.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

static class N
{
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr v);
    [DllImport("user32.dll")] public static extern bool UpdateLayeredWindow(IntPtr hwnd, IntPtr hdcDst, ref POINT pptDst, ref SIZE psize, IntPtr hdcSrc, ref POINT pprSrc, int crKey, ref BLEND pblend, int dwFlags);
    [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr h);
    [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr h, IntPtr dc);
    [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")] public static extern bool DeleteDC(IntPtr dc);
    [DllImport("gdi32.dll")] public static extern IntPtr SelectObject(IntPtr dc, IntPtr o);
    [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr o);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint f);
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetWindowDisplayAffinity(IntPtr h, uint a);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out int v, int size);
    [DllImport("user32.dll")] public static extern IntPtr SetWindowsHookEx(int id, HookProc fn, IntPtr mod, uint tid);
    [DllImport("user32.dll")] public static extern IntPtr CallNextHookEx(IntPtr h, int code, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern bool UnhookWindowsHookEx(IntPtr h);
    [DllImport("kernel32.dll")] public static extern IntPtr GetModuleHandle(string n);
    [DllImport("kernel32.dll")] public static extern uint GetTickCount();
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
    [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr h, uint f);
    [DllImport("user32.dll")] public static extern bool GetMonitorInfo(IntPtr m, ref MONITORINFO mi);
    public delegate IntPtr HookProc(int code, IntPtr w, IntPtr l);
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int x, y; public POINT(int a, int b) { x = a; y = b; } }
    [StructLayout(LayoutKind.Sequential)] public struct SIZE { public int cx, cy; public SIZE(int a, int b) { cx = a; cy = b; } }
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int l, t, r, b; }
    [StructLayout(LayoutKind.Sequential)] public struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }
    [StructLayout(LayoutKind.Sequential, Pack = 1)] public struct BLEND { public byte op, flags, alpha, fmt; }
    [StructLayout(LayoutKind.Sequential)] public struct KBD { public uint vk, scan, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] public struct MSLL { public POINT pt; public uint data, flags, time; public UIntPtr extra; }
    public static readonly IntPtr TOPMOST = new IntPtr(-1);
    public static readonly IntPtr TOP = IntPtr.Zero;
    public const uint NOSIZE = 1, NOMOVE = 2, NOACTIVATE = 0x10, SHOW = 0x40, HIDE = 0x80, NOOWNERZ = 0x200;
    public const uint HANDS_TAG = 0x524F5741;

    public static RECT Frame(IntPtr h)
    {
        RECT r;
        DwmGetWindowAttribute(h, 9, out r, Marshal.SizeOf(typeof(RECT)));
        return r;
    }
    public static bool Cloaked(IntPtr h)
    {
        int v;
        DwmGetWindowAttribute(h, 14, out v, 4);
        return v != 0;
    }
}

// A borderless per-pixel-alpha window; Render() draws into a bitmap, Push() blits it.
class Layer : Form
{
    readonly bool clickThrough, topmost;
    public Layer(bool clickThrough, bool topmost)
    {
        this.clickThrough = clickThrough;
        this.topmost = topmost;
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.Manual;
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams
    {
        get
        {
            CreateParams cp = base.CreateParams;
            cp.ExStyle |= 0x80000 | 0x80 | 0x08000000; // LAYERED | TOOLWINDOW | NOACTIVATE
            if (clickThrough) cp.ExStyle |= 0x20;      // TRANSPARENT
            if (topmost) cp.ExStyle |= 0x8;            // TOPMOST
            return cp;
        }
    }
    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        // WDA_EXCLUDEFROMCAPTURE: never in screenshots (HANDS_PROOF=1 lifts it for proof shots only)
        if (Environment.GetEnvironmentVariable("HANDS_PROOF") != "1") N.SetWindowDisplayAffinity(Handle, 0x11);
    }
    public void Push(Bitmap bmp, int x, int y, byte alpha)
    {
        IntPtr screen = N.GetDC(IntPtr.Zero), mem = N.CreateCompatibleDC(screen);
        IntPtr hb = bmp.GetHbitmap(Color.FromArgb(0)), old = N.SelectObject(mem, hb);
        N.SIZE sz = new N.SIZE(bmp.Width, bmp.Height);
        N.POINT src = new N.POINT(0, 0), dst = new N.POINT(x, y);
        N.BLEND bl = new N.BLEND(); bl.op = 0; bl.flags = 0; bl.alpha = alpha; bl.fmt = 1;
        N.UpdateLayeredWindow(Handle, screen, ref dst, ref sz, mem, ref src, 0, ref bl, 2);
        N.SelectObject(mem, old); N.DeleteObject(hb); N.DeleteDC(mem); N.ReleaseDC(IntPtr.Zero, screen);
    }
}

// Which input is the person's. Only REAL movement by a person counts: a key pressed down, a mouse button pressed, the
// wheel, or the mouse travelling a real distance. Ignored: key-ups alone, zero-distance moves, and echoes, i.e. a
// move that lands where the agent itself put the cursor a moment ago. Remote Desktop sends the agent's own cursor
// moves back to the machine as fresh, non-injected input (the client follows the server's pointer and reports
// where it now is), so without this the agent "felt the person's hands" every time it moved and let go.
// Pure logic, no Win32: the hooks feed it, and `overlay.exe --judge` feeds it from a test.
class InputJudge
{
    public const double STILL_PX = 1.5;   // a move shorter than this from where the cursor is: no movement at all
    public const double MOVE_PX = 8;      // her path must add up to this much before it counts as her hands moving
    public const uint PATH_GAP_MS = 300;  // a pause this long starts her path from zero again
    public const int ECHO_PX = 8;         // a point this close to one the agent put the cursor on is that echo
    public const uint ECHO_MS_LOCAL = 500, ECHO_MS_REMOTE = 3000;  // how long an echo can take to come back
    const int RING = 256;
    readonly int[] rx = new int[RING], ry = new int[RING]; readonly uint[] rt = new uint[RING];
    int rnext = 0, rcount = 0;
    double path = 0; uint lastMove = 0; bool anyMove = false;

    // a point the agent's own input (or any injected input) put the cursor on
    public void Mine(int x, int y, uint now)
    {
        rx[rnext] = x; ry[rnext] = y; rt[rnext] = now;
        rnext = (rnext + 1) % RING; if (rcount < RING) rcount++;
    }

    bool Echo(int x, int y, uint now, bool remote)
    {
        uint window = remote ? ECHO_MS_REMOTE : ECHO_MS_LOCAL;
        for (int i = 0; i < rcount; i++)
        {
            if (unchecked(now - rt[i]) > window) continue;
            if (Math.Abs(rx[i] - x) <= ECHO_PX && Math.Abs(ry[i] - y) <= ECHO_PX) return true;
        }
        return false;
    }

    // a mouse event that wasn't the agent's: null = not her hands, else what her hands did
    public string Mouse(int msg, int x, int y, int cx, int cy, uint now, bool remote)
    {
        switch (msg)
        {
            case 0x201: case 0x204: case 0x207: case 0x20B: return "mouse button";
            case 0x20A: case 0x20E: return "mouse wheel";
            case 0x200: break;
            default: return null;  // button-ups and the rest: a press already counted
        }
        double d = Math.Sqrt((double)(x - cx) * (x - cx) + (double)(y - cy) * (y - cy));
        if (d < STILL_PX) return null;              // a zero-distance move: nobody moved anything
        if (Echo(x, y, now, remote)) return null;   // the agent's own move coming back
        if (!anyMove || unchecked(now - lastMove) > PATH_GAP_MS) path = 0;
        anyMove = true; lastMove = now;
        path += d;
        if (path < MOVE_PX) return null;            // a one-pixel bump isn't a hand on the mouse
        string why = "mouse moved " + (int)Math.Round(path) + " px";
        path = 0;
        return why;
    }

    public string Key(int msg)
    {
        return (msg == 0x100 || msg == 0x104) ? "key pressed" : null;  // key-ups alone (a remote client's resync) aren't her
    }

    // test door: lines on stdin, one verdict per line on stdout ("hers <why>" | "no" | "ok")
    //   mine x y t | mouse msg x y cx cy t remote(0/1) | key msg
    public static void SelfTest()
    {
        InputJudge j = new InputJudge();
        string line;
        while ((line = Console.In.ReadLine()) != null)
        {
            string[] a = line.Trim().Split(' ');
            if (a.Length == 0 || a[0] == "") continue;
            string r;
            if (a[0] == "mine") { j.Mine(int.Parse(a[1]), int.Parse(a[2]), uint.Parse(a[3])); r = "ok"; }
            else if (a[0] == "mouse") r = j.Mouse(Convert.ToInt32(a[1], 16), int.Parse(a[2]), int.Parse(a[3]), int.Parse(a[4]), int.Parse(a[5]), uint.Parse(a[6]), a[7] == "1");
            else if (a[0] == "key") r = j.Key(Convert.ToInt32(a[1], 16));
            else r = "?";
            Console.Out.WriteLine(r == null ? "no" : (r == "ok" ? "ok" : "hers " + r));
            Console.Out.Flush();
        }
    }
}

class App
{
    // the agent's own look, from the engine (engine/identity.py); these defaults match it
    static readonly Color PINK = Look("HANDS_LOOK_COLOR", Color.FromArgb(255, 228, 0, 124));
    static readonly Color PINK_HOT = Look("HANDS_LOOK_COLOR_HOT", Color.FromArgb(255, 255, 77, 166));
    static readonly Color INK = Color.FromArgb(240, 20, 14, 22);
    static readonly string INITIAL = Env("HANDS_LOOK_INITIAL", "R", 2);
    static readonly string NAME = Env("HANDS_LOOK_NAME", "Rowan", 32);

    static string Env(string key, string fallback, int max)
    {
        string v = Environment.GetEnvironmentVariable(key);
        return string.IsNullOrWhiteSpace(v) ? fallback : (v.Length > max ? v.Substring(0, max) : v);
    }

    static Color Look(string key, Color fallback)
    {
        string v = Environment.GetEnvironmentVariable(key);
        int rgb;
        if (v != null && v.Length == 7 && v[0] == '#' &&
            int.TryParse(v.Substring(1), System.Globalization.NumberStyles.HexNumber, null, out rgb))
            return Color.FromArgb(255, (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255);
        return fallback;
    }

    Layer border, pill, cursor, card;
    List<Layer> glows = new List<Layer>(); bool glowOn = false; // the whole-screen glow, one layer per monitor
    IntPtr target = IntPtr.Zero;
    string label = Env("HANDS_LOOK_LABEL", "Rowan is working here", 48);
    bool fgMode = false, active = false;
    double phase = 0;
    // ghost cursor state
    float cx = -1, cy = -1, fromX, fromY, toX, toY;
    int glideSeq = -1; long glideStart = 0; int glideMs = 300; float bendX = 0, bendY = 0;
    long lastMove = 0, rippleStart = 0; string glideKind = "click";
    // consent
    int consentSeq = -1; string consentApp = "", consentDetail = ""; Rectangle bOnce, bAlways, bNo;
    // pill
    Rectangle stopRect; bool pillHover = false;
    // hooks
    IntPtr kbHook, msHook; N.HookProc kbProc, msProc; uint lastEsc = 0, lastHerSent = 0;
    readonly InputJudge judge = new InputJudge();
    readonly object outLock = new object();
    System.Windows.Forms.Timer timer;
    SynchronizationContext ui;
    Font fLabel, fSmall, fTitle, fBtn;

    static void Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--judge") { InputJudge.SelfTest(); return; }
        try { N.SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch { }
        Application.EnableVisualStyles();
        new App().Run();
    }

    void Run()
    {
        fLabel = new Font("Segoe UI Semibold", 10f);
        fSmall = new Font("Segoe UI", 8.5f);
        fTitle = new Font("Segoe UI Semibold", 11.5f);
        fBtn = new Font("Segoe UI Semibold", 9.5f);
        border = new Layer(true, false);
        cursor = new Layer(true, true);
        pill = new Layer(false, true);
        card = new Layer(false, true);
        pill.MouseMove += delegate (object s, MouseEventArgs e) { bool h = stopRect.Contains(e.Location); if (h != pillHover) { pillHover = h; } };
        pill.MouseLeave += delegate { pillHover = false; };
        pill.MouseDown += delegate (object s, MouseEventArgs e) { if (stopRect.Contains(e.Location)) Stop("pill"); };
        card.MouseDown += OnCardClick;
        foreach (Layer l in new Layer[] { border, cursor, pill, card }) { l.Show(); l.Hide(); }
        ui = new WindowsFormsSynchronizationContext();
        SynchronizationContext.SetSynchronizationContext(ui);
        timer = new System.Windows.Forms.Timer(); timer.Interval = 33; timer.Tick += delegate { Frame(); }; // 30 fps, only while showing
        timer.Start();
        kbProc = KbHook; msProc = MsHook;
        IntPtr mod = N.GetModuleHandle(null);
        kbHook = N.SetWindowsHookEx(13, kbProc, mod, 0);
        msHook = N.SetWindowsHookEx(14, msProc, mod, 0);
        Thread reader = new Thread(ReadLoop); reader.IsBackground = true; reader.Start();
        Emit("{\"ev\":\"ready\"}");
        Application.Run();
        N.UnhookWindowsHookEx(kbHook); N.UnhookWindowsHookEx(msHook);
    }

    // ------------------------------------------------------------- wire
    void Emit(string json)
    {
        lock (outLock) { try { Console.Out.WriteLine(json); Console.Out.Flush(); } catch { } }
    }

    void ReadLoop()
    {
        StreamReader inp = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
        JavaScriptSerializer js = new JavaScriptSerializer();
        string line;
        while ((line = inp.ReadLine()) != null)
        {
            Dictionary<string, object> m;
            try { m = js.Deserialize<Dictionary<string, object>>(line); } catch { continue; }
            ui.Post(delegate { Handle(m); }, null);
        }
        ui.Post(delegate { Application.Exit(); }, null); // parent gone: leave nothing behind
    }

    static int I(Dictionary<string, object> m, string k) { object v; return m.TryGetValue(k, out v) ? Convert.ToInt32(v) : 0; }
    static long L(Dictionary<string, object> m, string k) { object v; return m.TryGetValue(k, out v) ? Convert.ToInt64(v) : 0; }
    static string S(Dictionary<string, object> m, string k) { object v; return m.TryGetValue(k, out v) && v != null ? v.ToString() : ""; }

    void Handle(Dictionary<string, object> m)
    {
        string op = S(m, "op");
        if (!timer.Enabled) timer.Start();
        if (op == "target") { target = new IntPtr(L(m, "hwnd")); string lb = S(m, "label"); if (lb != "") label = lb; active = true; }
        else if (op == "mode") { object v; fgMode = m.TryGetValue("fg", out v) && Convert.ToBoolean(v); }
        else if (op == "awake") { active = true; } // working, in any window or none (a background tab, a screen look): the glow says so
        else if (op == "glide")
        {
            active = true;
            float x = I(m, "x"), y = I(m, "y");
            if (cx < 0) { cx = x - 90; cy = y - 120; }
            fromX = cx; fromY = cy; toX = x; toY = y; glideSeq = I(m, "seq"); glideKind = S(m, "kind");
            // travel the person can follow: duration by distance, along a soft curve bending to one side
            double dist = Math.Sqrt((toX - fromX) * (toX - fromX) + (toY - fromY) * (toY - fromY));
            glideMs = (int)Math.Max(200, Math.Min(520, 170 + dist * 0.35));
            float side = ((glideSeq & 1) == 0) ? 1f : -1f;
            bendX = (float)(-(toY - fromY) * 0.18 * side); bendY = (float)((toX - fromX) * 0.18 * side);
            glideStart = Environment.TickCount; lastMove = glideStart;
        }
        else if (op == "consent") { consentSeq = I(m, "seq"); consentApp = S(m, "app"); consentDetail = S(m, "detail"); }
        else if (op == "consent_cancel") { if (I(m, "seq") == consentSeq) consentSeq = -1; }
        else if (op == "idle") { active = false; fgMode = false; target = IntPtr.Zero; }
        else if (op == "quit") { Application.Exit(); }
    }

    void Stop(string why)
    {
        Emit("{\"ev\":\"stop\",\"why\":\"" + why + "\"}");
        active = false; fgMode = false; target = IntPtr.Zero;
    }

    // ------------------------------------------------------------- the person's hands vs the agent's
    IntPtr KbHook(int code, IntPtr w, IntPtr l)
    {
        if (code >= 0)
        {
            N.KBD k = (N.KBD)Marshal.PtrToStructure(l, typeof(N.KBD));
            bool injected = (k.flags & 0x12) != 0;  // LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
            if (!injected && (k.extra.ToUInt64() != N.HANDS_TAG))
            {
                string why = judge.Key(w.ToInt32());
                if (why != null) HerInput(why);
                bool down = w.ToInt32() == 0x100 || w.ToInt32() == 0x104;
                if (down && k.vk == 0x1B && (active || consentSeq >= 0))
                {
                    uint now = N.GetTickCount();
                    bool twice = now - lastEsc < 600;
                    if (consentSeq >= 0 && twice)
                    {
                        Emit("{\"ev\":\"consent\",\"seq\":" + consentSeq + ",\"choice\":\"no\"}");
                        consentSeq = -1; lastEsc = 0;
                    }
                    else if (active && (fgMode || twice)) { Stop("esc"); lastEsc = 0; }
                    else lastEsc = now;
                }
            }
        }
        return N.CallNextHookEx(kbHook, code, w, l);
    }

    IntPtr MsHook(int code, IntPtr w, IntPtr l)
    {
        if (code >= 0)
        {
            N.MSLL m = (N.MSLL)Marshal.PtrToStructure(l, typeof(N.MSLL));
            bool injected = (m.flags & 0x3) != 0;  // LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED
            uint now = N.GetTickCount();
            if (injected || m.extra.ToUInt64() == N.HANDS_TAG) judge.Mine(m.pt.x, m.pt.y, now);
            else
            {
                N.POINT cur; N.GetCursorPos(out cur);  // the hook runs before the cursor moves: this is where it is now
                bool remote = N.GetSystemMetrics(0x1000) != 0;  // SM_REMOTESESSION: Remote Desktop echoes moves back
                string why = judge.Mouse(w.ToInt32(), m.pt.x, m.pt.y, cur.x, cur.y, now, remote);
                if (why != null) HerInput(why);
            }
        }
        return N.CallNextHookEx(msHook, code, w, l);
    }

    void HerInput(string why)
    {
        uint now = N.GetTickCount();
        if (now - lastHerSent > 100) { lastHerSent = now; Emit("{\"ev\":\"her_input\",\"tick\":" + now + ",\"why\":\"" + why + "\"}"); }
    }

    // ------------------------------------------------------------- frame loop
    void Frame()
    {
        phase += 0.016;
        long now = Environment.TickCount;
        bool haveTarget = active && target != IntPtr.Zero && N.IsWindow(target) && N.IsWindowVisible(target) && !N.IsIconic(target) && !N.Cloaked(target);
        SetGlow(active);
        if (haveTarget) DrawBorder(); else if (border.Visible) border.Hide();
        if (haveTarget) DrawPill(); else if (pill.Visible) pill.Hide();
        DrawCursor(now);
        if (consentSeq >= 0) DrawCard(); else if (card.Visible) card.Hide();
        if (!border.Visible && !pill.Visible && !cursor.Visible && !card.Visible && !active && consentSeq < 0)
            timer.Stop(); // nothing on the screen: zero work until the next op
    }

    // The whole-screen glow: a faint deep-pink edge round every monitor, fading inward, while I'm working.
    // Drawn once per monitor and left static (no per-frame redraw), click-through, never in a capture.
    void SetGlow(bool on)
    {
        if (on == glowOn) return;
        glowOn = on;
        if (!on) { foreach (Layer g in glows) g.Hide(); return; }
        if (glows.Count == 0)
        {
            foreach (Screen s in Screen.AllScreens)
            {
                Layer l = new Layer(true, true);
                l.Show(); l.Hide();
                Rectangle r = s.Bounds;
                using (Bitmap bmp = new Bitmap(r.Width, r.Height, PixelFormat.Format32bppPArgb))
                using (Graphics g = Graphics.FromImage(bmp))
                {
                    const int D = 34; // how far the glow reaches inward
                    for (int i = 0; i < D; i++)
                    {
                        int a = (int)(150 * Math.Pow(1 - i / (double)D, 2.4));
                        using (Pen p = new Pen(Color.FromArgb(a, PINK), 1f))
                            g.DrawRectangle(p, i, i, r.Width - 1 - 2 * i, r.Height - 1 - 2 * i);
                    }
                    l.Push(bmp, r.X, r.Y, 255);
                }
                glows.Add(l);
            }
        }
        foreach (Layer l in glows)
        {
            l.Show();
            N.SetWindowPos(l.Handle, N.TOPMOST, 0, 0, 0, 0, N.NOACTIVATE | N.SHOW | N.NOSIZE | N.NOMOVE);
        }
    }

    static GraphicsPath Round(RectangleF r, float rad)
    {
        GraphicsPath p = new GraphicsPath();
        float d = rad * 2;
        p.AddArc(r.X, r.Y, d, d, 180, 90); p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90); p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        p.CloseFigure();
        return p;
    }

    void ZJustAbove(Layer l, int x, int y, int w, int h)
    {
        IntPtr prev = N.GetWindow(target, 3); // GW_HWNDPREV: the window just above the target
        while (prev != IntPtr.Zero && (prev == border.Handle || prev == pill.Handle || prev == cursor.Handle || prev == card.Handle))
            prev = N.GetWindow(prev, 3);
        N.SetWindowPos(l.Handle, prev == IntPtr.Zero ? N.TOP : prev, x, y, w, h, N.NOACTIVATE | N.SHOW | N.NOOWNERZ);
    }

    void DrawBorder()
    {
        N.RECT r = N.Frame(target);
        const int M = 10;
        int w = r.r - r.l + 2 * M, h = r.b - r.t + 2 * M;
        if (w < 40 || h < 40) return;
        using (Bitmap bmp = new Bitmap(w, h, PixelFormat.Format32bppPArgb))
        using (Graphics g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            double breathe = 0.65 + 0.35 * Math.Sin(phase * (fgMode ? 5.0 : 2.2));
            for (int i = M; i >= 1; i--)
            {
                int a = (int)(breathe * 150 * Math.Pow((M - i + 1) / (double)M, 2.2));
                using (Pen p = new Pen(Color.FromArgb(Math.Min(255, a), fgMode ? PINK_HOT : PINK), 1.6f))
                using (GraphicsPath gp = Round(new RectangleF(M - i, M - i, w - 2 * (M - i) - 1, h - 2 * (M - i) - 1), 8 + i))
                    g.DrawPath(p, gp);
            }
            using (Pen core = new Pen(Color.FromArgb((int)(200 * breathe + 55), PINK_HOT), 2f))
            using (GraphicsPath gp = Round(new RectangleF(M - 1, M - 1, w - 2 * M + 1, h - 2 * M + 1), 8))
                g.DrawPath(core, gp);
            border.Push(bmp, r.l - M, r.t - M, 255);
        }
        ZJustAbove(border, r.l - M, r.t - M, w, h);
    }

    void DrawPill()
    {
        N.RECT r = N.Frame(target);
        string main = fgMode ? NAME + " has the mouse for a moment" : label;
        string hint = fgMode ? "Esc to stop" : "Esc Esc to stop";
        int w, h = 34;
        using (Bitmap probe = new Bitmap(1, 1)) using (Graphics pg = Graphics.FromImage(probe))
            w = (int)(pg.MeasureString(main, fLabel).Width + pg.MeasureString(hint, fSmall).Width) + 104;
        // float just above the window when there's room, so it never covers the app's own title row
        IntPtr mon = N.MonitorFromWindow(target, 2);
        N.MONITORINFO mi = new N.MONITORINFO(); mi.cbSize = Marshal.SizeOf(typeof(N.MONITORINFO));
        N.GetMonitorInfo(mon, ref mi);
        int x = (r.l + r.r) / 2 - w / 2, y = r.t - h - 12;
        if (y < mi.rcWork.t + 2) y = r.t + 8;
        using (Bitmap bmp = new Bitmap(w, h, PixelFormat.Format32bppPArgb))
        using (Graphics g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias; g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            using (GraphicsPath gp = Round(new RectangleF(0.5f, 0.5f, w - 1, h - 1), 16.5f))
            {
                using (SolidBrush b = new SolidBrush(INK)) g.FillPath(b, gp);
                using (Pen p = new Pen(Color.FromArgb(200, PINK), 1.2f)) g.DrawPath(p, gp);
            }
            // wolf eye: a pink dot that breathes
            float pulse = (float)(0.6 + 0.4 * Math.Sin(phase * 3));
            using (SolidBrush b = new SolidBrush(Color.FromArgb((int)(255 * pulse), PINK_HOT))) g.FillEllipse(b, 13, 12, 10, 10);
            g.DrawString(main, fLabel, Brushes.White, 30, 8);
            float mw = g.MeasureString(main, fLabel).Width;
            using (SolidBrush b = new SolidBrush(Color.FromArgb(170, 255, 255, 255))) g.DrawString(hint, fSmall, b, 34 + mw, 10);
            stopRect = new Rectangle(w - 58, 6, 50, 22);
            using (GraphicsPath sp = Round(stopRect, 11))
            {
                using (SolidBrush b = new SolidBrush(pillHover ? PINK : Color.FromArgb(60, PINK))) g.FillPath(b, sp);
                using (Pen p = new Pen(PINK, 1f)) g.DrawPath(p, sp);
            }
            g.DrawString("Stop", fSmall, Brushes.White, stopRect.X + 11, stopRect.Y + 3);
            pill.Push(bmp, x, y, 255);
        }
        if (!pill.Visible) { pill.Show(); }
        N.SetWindowPos(pill.Handle, N.TOPMOST, x, y, w, h, N.NOACTIVATE | N.SHOW);
    }

    void DrawCursor(long now)
    {
        if (cx < 0) { if (cursor.Visible) cursor.Hide(); return; }
        bool moving = glideSeq >= 0;
        if (moving)
        {
            double t = Math.Min(1.0, (now - glideStart) / (double)glideMs);
            double e = t < 0.5 ? 4 * t * t * t : 1 - Math.Pow(-2 * t + 2, 3) / 2; // ease in-out
            // a soft curve: the midpoint is pushed sideways by (bendX, bendY), fading to zero at both ends
            double arc = 4 * e * (1 - e);
            cx = (float)(fromX + (toX - fromX) * e + bendX * arc);
            cy = (float)(fromY + (toY - fromY) * e + bendY * arc);
            if (t >= 1.0)
            {
                Emit("{\"ev\":\"arrived\",\"seq\":" + glideSeq + "}");
                glideSeq = -1; rippleStart = now; lastMove = now;
            }
        }
        long since = now - lastMove;
        int alpha = since < 2600 ? 255 : Math.Max(0, 255 - (int)((since - 2600) / 3));
        if (alpha <= 0 || !active) { if (cursor.Visible) cursor.Hide(); if (!active) cx = -1; return; }
        const int S = 96;
        const float ox = 30, oy = 30; // the tip sits at (ox, oy)
        const float SIZE = 1.35f;     // big enough that the ears read at a glance
        long rs = now - rippleStart;
        // alive: a gentle wiggle while I'm working (not while travelling), a squish when I press
        // the idle wiggle: the tip stays put and the tail sways, a quick little double shimmy
        double wx = now / 170.0;
        double wig = moving ? 0 : (Math.Sin(wx) + 0.35 * Math.Sin(2 * wx)) * 6.5 * Math.Min(1.0, since / 400.0);
        float squish = (!moving && rs < 160 && glideKind != "look") ? (float)(0.82 + 0.18 * (rs / 160.0)) : 1f;
        using (Bitmap bmp = new Bitmap(S, S, PixelFormat.Format32bppPArgb))
        using (Graphics g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            if (rs < 520 && !moving && glideKind != "look")
            {
                float rr = 4 + rs / 16f; int ra = (int)(210 * (1 - rs / 520.0));
                using (Pen p = new Pen(Color.FromArgb(ra, PINK_HOT), 2.2f)) g.DrawEllipse(p, ox - rr, oy - rr, rr * 2, rr * 2);
            }
            g.TranslateTransform(ox, oy);
            g.RotateTransform((float)wig);
            g.ScaleTransform(squish * SIZE, squish * SIZE);
            // The cursor: a simple triangle pointer with a thick rounded edge. The fill is solid pink, no glow;
            // the EDGE is iridescent (pink -> violet -> ice blue, slowly turning) and the glow lives on the edge only.
            PointF[] tri = { new PointF(0, 0), new PointF(0, 23), new PointF(6.6f, 16.4f), new PointF(16.6f, 16.4f) };
            using (GraphicsPath gp = new GraphicsPath())
            {
                gp.AddPolygon(tri);
                using (SolidBrush sh = new SolidBrush(Color.FromArgb(60, 0, 0, 0))) { g.TranslateTransform(1.0f, 1.5f); g.FillPath(sh, gp); g.TranslateTransform(-1.0f, -1.5f); }
                double turn = (now / 900.0) % (2 * Math.PI);
                PointF a = new PointF((float)(8 + 12 * Math.Cos(turn)), (float)(11 + 12 * Math.Sin(turn)));
                PointF b2 = new PointF((float)(8 - 12 * Math.Cos(turn)), (float)(11 - 12 * Math.Sin(turn)));
                ColorBlend iris = new ColorBlend
                {
                    Colors = new[] { Color.FromArgb(255, 255, 120, 196), Color.FromArgb(255, 190, 120, 255),
                                     Color.FromArgb(255, 130, 210, 255), Color.FromArgb(255, 255, 150, 220) },
                    Positions = new[] { 0f, 0.38f, 0.7f, 1f }
                };
                using (LinearGradientBrush edge = new LinearGradientBrush(a, b2, Color.White, Color.White))
                {
                    edge.InterpolationColors = iris;
                    for (int i = 3; i >= 1; i--)   // the glow, on the edge only
                        using (Pen glow = new Pen(Color.FromArgb(46, 200, 130, 255), 3.0f + i * 2.4f)) { glow.LineJoin = LineJoin.Round; g.DrawPath(glow, gp); }
                    using (SolidBrush fill = new SolidBrush(PINK)) g.FillPath(fill, gp);
                    using (Pen p = new Pen(edge, 2.6f)) { p.LineJoin = LineJoin.Round; p.StartCap = p.EndCap = LineCap.Round; g.DrawPath(p, gp); }
                }
            }
            // the badge: the agent's initial in a little wolf head (two ears), so nobody mistakes it for the person's cursor
            float bx = 13.5f, by = 18.5f;
            PointF[] earL = { new PointF(bx + 1.5f, by + 4), new PointF(bx + 3f, by - 1.8f), new PointF(bx + 6.5f, by + 2.2f) };
            PointF[] earR = { new PointF(bx + 9.5f, by + 2.2f), new PointF(bx + 13f, by - 1.8f), new PointF(bx + 14.5f, by + 4) };
            using (SolidBrush b = new SolidBrush(INK)) { g.FillPolygon(b, earL); g.FillPolygon(b, earR); g.FillEllipse(b, bx, by, 16, 16); }
            using (Pen p = new Pen(PINK, 1.1f)) { p.LineJoin = LineJoin.Round; g.DrawPolygon(p, earL); g.DrawPolygon(p, earR); g.DrawEllipse(p, bx, by, 16, 16); }
            using (SolidBrush b = new SolidBrush(INK)) g.FillEllipse(b, bx + 1.2f, by + 1.2f, 13.6f, 13.6f);
            using (Font f = new Font("Segoe UI Black", 7.6f)) g.DrawString(INITIAL, f, Brushes.White, bx + 3.1f, by + 1.6f);
            g.ResetTransform();
            cursor.Push(bmp, (int)cx - (int)ox, (int)cy - (int)oy, (byte)alpha);
        }
        if (!cursor.Visible) cursor.Show();
        N.SetWindowPos(cursor.Handle, N.TOPMOST, (int)cx - (int)ox, (int)cy - (int)oy, S, S, N.NOACTIVATE | N.SHOW | N.NOSIZE);
    }

    void DrawCard()
    {
        IntPtr mon = N.MonitorFromWindow(target != IntPtr.Zero ? target : N.GetForegroundWindow(), 2);
        N.MONITORINFO mi = new N.MONITORINFO(); mi.cbSize = Marshal.SizeOf(typeof(N.MONITORINFO));
        N.GetMonitorInfo(mon, ref mi);
        const int w = 400, h = 150;
        int x = mi.rcWork.r - w - 24, y = mi.rcWork.b - h - 24;
        using (Bitmap bmp = new Bitmap(w, h, PixelFormat.Format32bppPArgb))
        using (Graphics g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias; g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            using (GraphicsPath gp = Round(new RectangleF(0.5f, 0.5f, w - 1, h - 1), 14))
            {
                using (SolidBrush b = new SolidBrush(Color.FromArgb(248, 20, 14, 22))) g.FillPath(b, gp);
                double br = 0.6 + 0.4 * Math.Sin(phase * 3);
                using (Pen p = new Pen(Color.FromArgb((int)(255 * br), PINK), 1.6f)) g.DrawPath(p, gp);
            }
            using (SolidBrush b = new SolidBrush(PINK_HOT)) g.FillEllipse(b, 18, 22, 10, 10);
            g.DrawString(NAME + " wants to use " + Trim(consentApp, 26), fTitle, Brushes.White, 36, 15);
            using (SolidBrush b = new SolidBrush(Color.FromArgb(160, 255, 255, 255))) g.DrawString(Trim(consentDetail, 60), fSmall, b, 37, 44);
            using (SolidBrush b = new SolidBrush(Color.FromArgb(120, 255, 255, 255))) g.DrawString("Only you can answer this. Esc Esc = No.", fSmall, b, 37, 64);
            bNo = new Rectangle(18, 102, 92, 32); bOnce = new Rectangle(w - 228, 102, 104, 32); bAlways = new Rectangle(w - 114, 102, 96, 32);
            Button(g, bNo, "No", false); Button(g, bOnce, "Allow once", false); Button(g, bAlways, "Always", true);
            card.Push(bmp, x, y, 255);
        }
        if (!card.Visible) card.Show();
        N.SetWindowPos(card.Handle, N.TOPMOST, x, y, w, h, N.NOACTIVATE | N.SHOW);
    }

    void Button(Graphics g, Rectangle r, string text, bool primary)
    {
        using (GraphicsPath gp = Round(r, 16))
        {
            using (SolidBrush b = new SolidBrush(primary ? PINK : Color.FromArgb(40, 255, 255, 255))) g.FillPath(b, gp);
            using (Pen p = new Pen(primary ? PINK_HOT : Color.FromArgb(90, 255, 255, 255), 1f)) g.DrawPath(p, gp);
        }
        SizeF s = g.MeasureString(text, fBtn);
        g.DrawString(text, fBtn, Brushes.White, r.X + (r.Width - s.Width) / 2, r.Y + (r.Height - s.Height) / 2);
    }

    static string Trim(string s, int n) { return s.Length <= n ? s : s.Substring(0, n - 1) + "…"; }

    void OnCardClick(object s, MouseEventArgs e)
    {
        if (consentSeq < 0) return;
        string choice = null;
        if (bOnce.Contains(e.Location)) choice = "once";
        else if (bAlways.Contains(e.Location)) choice = "always";
        else if (bNo.Contains(e.Location)) choice = "no";
        if (choice == null) return;
        Emit("{\"ev\":\"consent\",\"seq\":" + consentSeq + ",\"choice\":\"" + choice + "\"}");
        consentSeq = -1;
    }
}
