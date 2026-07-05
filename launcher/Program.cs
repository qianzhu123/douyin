using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

namespace DouyinLauncher
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            try
            {
                var baseDirectory = AppDomain.CurrentDomain.BaseDirectory;
                var projectRoot = FindProjectRoot(baseDirectory);
                var scriptPath = Path.Combine(projectRoot, "scripts", "start-douyin.vbs");
                if (!File.Exists(scriptPath))
                {
                    MessageBox.Show("找不到启动脚本：" + scriptPath, "Douyin", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }

                Process.Start(new ProcessStartInfo
                {
                    FileName = "wscript.exe",
                    Arguments = "\"" + scriptPath + "\"",
                    WorkingDirectory = projectRoot,
                    UseShellExecute = false,
                    CreateNoWindow = true
                });
            }
            catch (Exception ex)
            {
                MessageBox.Show(ex.Message, "Douyin", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private static string FindProjectRoot(string startDirectory)
        {
            var directory = new DirectoryInfo(startDirectory);
            while (directory != null)
            {
                if (Directory.Exists(Path.Combine(directory.FullName, "backend")) &&
                    File.Exists(Path.Combine(directory.FullName, "package.json")))
                {
                    return directory.FullName;
                }

                directory = directory.Parent;
            }

            return Path.GetFullPath(Path.Combine(startDirectory, "..", "..", ".."));
        }
    }
}
