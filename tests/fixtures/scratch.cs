// The tests' own scratch app: a text box that asks "save changes?" on close when dirty. It exists so no test ever
// has to type into an app the person uses. Title comes from HANDS_SCRATCH_TITLE (default "Hands scratch").
using System;
using System.Windows.Forms;

class Scratch : Form
{
    TextBox box = new TextBox();
    bool dirty = false;
    string baseTitle;

    Scratch()
    {
        baseTitle = Environment.GetEnvironmentVariable("HANDS_SCRATCH_TITLE") ?? "Hands scratch";
        Text = baseTitle; Width = 420; Height = 220;
        box.Multiline = true; box.Dock = DockStyle.Fill; box.AccessibleName = "scratch text";
        box.TextChanged += (s, e) => { dirty = true; Text = "*" + baseTitle; };
        Controls.Add(box);
        FormClosing += (s, e) =>
        {
            if (!dirty) return;
            var r = MessageBox.Show(this, "Save changes?", "Hands scratch", MessageBoxButtons.YesNoCancel);
            if (r == DialogResult.Cancel || r == DialogResult.Yes) e.Cancel = true;
        };
    }

    [STAThread]
    static void Main() { Application.EnableVisualStyles(); Application.Run(new Scratch()); }
}
