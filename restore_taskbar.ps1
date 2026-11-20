# Recovery helper in case the app was force-closed while the Windows taskbar was hidden.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class TaskbarRestore {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string className, string windowName);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hwnd, int command);
}
"@
$taskbar = [TaskbarRestore]::FindWindow("Shell_TrayWnd", $null)
if ($taskbar -ne [IntPtr]::Zero) {
    [void][TaskbarRestore]::ShowWindow($taskbar, 5)
    Write-Host "Barra de Windows restaurada."
} else {
    Write-Host "No se encontró la barra principal de Windows."
}
