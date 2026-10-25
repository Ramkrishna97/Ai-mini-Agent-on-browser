using System;
using System.Diagnostics;
using System.IO;

public static class Launcher
{
    public static int Main()
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string runtime = Path.Combine(root, "runtime");
            string app = Path.Combine(root, "app");
            var start = new ProcessStartInfo(Path.Combine(runtime, "python.exe"));
            start.Arguments = "\"" + Path.Combine(app, "ui_server.py") + "\"";
            start.WorkingDirectory = app;
            start.UseShellExecute = false;
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONPATH");
            start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            start.EnvironmentVariables["PATH"] = runtime + ";" + Path.Combine(runtime, "Scripts") + ";" + start.EnvironmentVariables["PATH"];
            Console.Title = "Local Workflow - Ctrl+C to stop";
            // The Python child shares this console and handles Ctrl+C itself.
            Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; };
            using (Process child = Process.Start(start))
            {
                child.WaitForExit();
                if (child.ExitCode != 0)
                {
                    Console.WriteLine("Application exited with code " + child.ExitCode + ". Press Enter to close.");
                    Console.ReadLine();
                }
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Unable to start: " + error.Message);
            Console.Error.WriteLine("Extract the entire ZIP into a writable folder, then try again. Press Enter to close.");
            Console.ReadLine();
            return 1;
        }
    }
}
