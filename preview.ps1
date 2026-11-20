$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Drawing;
public struct OroRect { public int Left, Top, Right, Bottom; }
public static class OroPreviewNative {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr FindWindow(string className, string windowName);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hwnd, out OroRect rect);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")]
    private static extern short GetAsyncKeyState(int key);
    public static bool IsDown(int key) { return (GetAsyncKeyState(key) & 0x8000) != 0; }
}
"@

$script:TaskbarHwnd = [OroPreviewNative]::FindWindow("Shell_TrayWnd", $null)
$script:PanelVisible = $false
$script:ToggleWasDown = $false
$script:QuitWasDown = $false
$script:EscWasDown = $false
$script:Panel = New-Object System.Windows.Forms.Form
$script:Panel.Text = "OroControl · Vista previa"
$script:Panel.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$script:Panel.ShowInTaskbar = $false
$script:Panel.TopMost = $true
$script:Panel.BackColor = [System.Drawing.Color]::FromArgb(16, 21, 27)
$script:Panel.KeyPreview = $true
$script:Panel.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::None

$flow = New-Object System.Windows.Forms.FlowLayoutPanel
$flow.Dock = [System.Windows.Forms.DockStyle]::Fill
$flow.FlowDirection = [System.Windows.Forms.FlowDirection]::TopDown
$flow.WrapContents = $false
$flow.AutoScroll = $false
$flow.Padding = New-Object System.Windows.Forms.Padding(1)
$flow.BackColor = $script:Panel.BackColor
$script:Panel.Controls.Add($flow)
$script:PreviewFlow = $flow
$script:PreviewControls = New-Object System.Collections.ArrayList
$script:PreviewSpacers = New-Object System.Collections.ArrayList

function Add-PreviewLabel([string]$Text, [int]$Height, [float]$Size, [System.Drawing.Color]$Color, [bool]$Bold = $false) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $Text
    $label.Width = 50
    $label.Height = $Height
    $label.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
    $label.AutoEllipsis = $true
    $label.ForeColor = $Color
    $label.BackColor = $script:Panel.BackColor
    $style = if ($Bold) { [System.Drawing.FontStyle]::Bold } else { [System.Drawing.FontStyle]::Regular }
    $label.Font = New-Object System.Drawing.Font("Segoe UI", $Size, $style)
    $label.Margin = New-Object System.Windows.Forms.Padding(0, 3, 0, 3)
    [void]$flow.Controls.Add($label)
    [void]$script:PreviewControls.Add($label)
    return $label
}

function Add-PreviewButton([string]$Text, [System.Drawing.Color]$Color) {
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.Width = 50
    $button.Height = 34
    $button.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $button.AutoEllipsis = $true
    $button.FlatAppearance.BorderSize = 0
    $button.BackColor = $Color
    $button.ForeColor = [System.Drawing.Color]::White
    $button.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
    $button.Enabled = $false
    $button.Margin = New-Object System.Windows.Forms.Padding(0, 4, 0, 4)
    [void]$flow.Controls.Add($button)
    [void]$script:PreviewControls.Add($button)
}

function Add-PreviewSpacer([int]$Height) {
    $spacer = New-Object System.Windows.Forms.Label
    $spacer.Text = ""
    $spacer.Width = 50
    $spacer.Height = $Height
    $spacer.Margin = New-Object System.Windows.Forms.Padding(0)
    $spacer.BackColor = $script:Panel.BackColor
    [void]$flow.Controls.Add($spacer)
    [void]$script:PreviewControls.Add($spacer)
    [void]$script:PreviewSpacers.Add($spacer)
}

$spreadPreviewText = @('SPREAD', '18 PT') -join [Environment]::NewLine
Add-PreviewLabel -Text "XAU" -Height 16 -Size 9 -Color ([System.Drawing.Color]::White) -Bold:$true | Out-Null
Add-PreviewLabel -Text "VISTA PREVIA" -Height 14 -Size 7 -Color ([System.Drawing.Color]::FromArgb(241, 178, 70)) -Bold:$true | Out-Null
$script:PricePreviewLabel = Add-PreviewLabel -Text "—" -Height 24 -Size 13 -Color ([System.Drawing.Color]::FromArgb(228, 235, 242)) -Bold:$true
Add-PreviewLabel -Text "MT5 OFF" -Height 14 -Size 7 -Color ([System.Drawing.Color]::FromArgb(145, 157, 169)) -Bold:$false | Out-Null
$script:SpreadPreviewLabel = Add-PreviewLabel -Text $spreadPreviewText -Height 24 -Size 7 -Color ([System.Drawing.Color]::FromArgb(160, 170, 180)) -Bold:$false
Add-PreviewSpacer 4

foreach ($fieldName in @("LOTE", "SL PX", "TP PX")) {
    $shortFieldName = switch ($fieldName) { "LOTE" { "LOT" } "SL PX" { "SL" } default { "TP" } }
    Add-PreviewLabel -Text $shortFieldName -Height 14 -Size 7 -Color ([System.Drawing.Color]::FromArgb(154, 166, 178)) -Bold:$false | Out-Null
    $field = New-Object System.Windows.Forms.TextBox
    $field.Width = 46
    $field.Height = 18
    if ($fieldName -eq "LOTE") { $field.Text = "0.01" } else { $field.Text = "-" }
    $field.ReadOnly = $true
    $field.TextAlign = [System.Windows.Forms.HorizontalAlignment]::Center
    $field.BackColor = [System.Drawing.Color]::FromArgb(32, 42, 51)
    $field.ForeColor = [System.Drawing.Color]::FromArgb(205, 214, 222)
    $field.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
    $field.Font = New-Object System.Drawing.Font("Segoe UI", 8)
    $field.Margin = New-Object System.Windows.Forms.Padding(0, 1, 0, 2)
    [void]$flow.Controls.Add($field)
    [void]$script:PreviewControls.Add($field)
}
Add-PreviewSpacer 5
Add-PreviewLabel -Text "RIESGO SL" -Height 14 -Size 7 -Color ([System.Drawing.Color]::FromArgb(208, 169, 92)) -Bold:$true | Out-Null
$riskPreviewText = @('BUY $8.20', 'SELL —') -join [Environment]::NewLine
$script:RiskPreviewValue = Add-PreviewLabel -Text $riskPreviewText -Height 28 -Size 7 -Color ([System.Drawing.Color]::FromArgb(208, 169, 92)) -Bold:$false

Add-PreviewButton "BUY" ([System.Drawing.Color]::FromArgb(20, 138, 81))
Add-PreviewButton "SELL" ([System.Drawing.Color]::FromArgb(198, 69, 69))
Add-PreviewButton "BE" ([System.Drawing.Color]::FromArgb(41, 51, 61))
Add-PreviewButton "CRUZ" ([System.Drawing.Color]::FromArgb(52, 66, 79))
Add-PreviewButton "SALIR" ([System.Drawing.Color]::FromArgb(132, 46, 53))
Add-PreviewSpacer 4
Add-PreviewLabel -Text "OPERACIÓN" -Height 14 -Size 7 -Color ([System.Drawing.Color]::FromArgb(205, 214, 222)) -Bold:$true | Out-Null
$positionPreviewText = @('BUY · GANANDO', '+$12.40') -join [Environment]::NewLine
$script:PositionPreviewLabel = Add-PreviewLabel -Text $positionPreviewText -Height 26 -Size 7.5 -Color ([System.Drawing.Color]::FromArgb(87, 217, 154)) -Bold:$true
Add-PreviewLabel -Text "SL + TP OK" -Height 14 -Size 7 -Color ([System.Drawing.Color]::FromArgb(87, 217, 154)) -Bold:$true | Out-Null
Add-PreviewLabel -Text "NIVELES" -Height 14 -Size 7 -Color ([System.Drawing.Color]::FromArgb(205, 214, 222)) -Bold:$true | Out-Null
$levelsPreviewText = @('ENT 4031.20', 'SL 4028.00', 'A SL +3.20', '+320 pt', 'TP 4040.00', 'A TP +8.80', '+880 pt') -join [Environment]::NewLine
$script:LevelsPreviewLabel = Add-PreviewLabel -Text $levelsPreviewText -Height 70 -Size 7 -Color ([System.Drawing.Color]::FromArgb(160, 170, 180)) -Bold:$false
Add-PreviewLabel -Text "BE APLICADO" -Height 14 -Size 7 -Color ([System.Drawing.Color]::FromArgb(87, 217, 154)) -Bold:$true | Out-Null
Add-PreviewSpacer 4
$script:DateTimeLabel = Add-PreviewLabel -Text "--/--/----`n--:--:--" -Height 30 -Size 6.5 -Color ([System.Drawing.Color]::FromArgb(160, 170, 180)) -Bold:$false
$shortcutText = @('CTRL+ALT+G', 'ESC VOLVER') -join [Environment]::NewLine
$script:ShortcutPreviewLabel = Add-PreviewLabel -Text $shortcutText -Height 26 -Size 6 -Color ([System.Drawing.Color]::FromArgb(160, 170, 180)) -Bold:$false

function Set-PreviewResponsive {
    $availableWidth = [Math]::Max(42, $script:PreviewFlow.ClientSize.Width - 4)
    $smallWidth = $availableWidth -lt 58
    foreach ($control in $script:PreviewControls) {
        $control.Width = $availableWidth
        $control.Margin = New-Object System.Windows.Forms.Padding(0, 1, 0, 1)
        if ($control -is [System.Windows.Forms.Label] -and $script:PreviewSpacers.Contains($control)) {
            $control.Height = 4
        } elseif ($control -is [System.Windows.Forms.Button]) {
            $control.Height = 24
            $control.Font = New-Object System.Drawing.Font("Segoe UI", 7.5, [System.Drawing.FontStyle]::Bold)
        } elseif ($control -is [System.Windows.Forms.TextBox]) {
            $control.Height = 18
            $control.Font = New-Object System.Drawing.Font("Segoe UI", 8)
        } elseif ($control -is [System.Windows.Forms.Label]) {
            if ($control -eq $script:DateTimeLabel) {
                $control.Height = 30
                $control.Font = New-Object System.Drawing.Font("Segoe UI", $(if ($smallWidth) { 5.5 } else { 6.5 }))
            } elseif ($control -eq $script:PricePreviewLabel) {
                $control.Height = 24
                $control.Font = New-Object System.Drawing.Font("Segoe UI", $(if ($smallWidth) { 11 } else { 13 }), [System.Drawing.FontStyle]::Bold)
            } elseif ($control -eq $script:LevelsPreviewLabel) {
                $control.Height = 70
                $control.Font = New-Object System.Drawing.Font("Segoe UI", $(if ($smallWidth) { 6 } else { 7 }))
            } elseif ($control -eq $script:PositionPreviewLabel) {
                $control.Height = 26
                $control.Font = New-Object System.Drawing.Font("Segoe UI", $(if ($smallWidth) { 6.5 } else { 7.5 }), [System.Drawing.FontStyle]::Bold)
            } elseif ($control -eq $script:SpreadPreviewLabel) {
                $control.Height = 24
                $control.Font = New-Object System.Drawing.Font("Segoe UI", $(if ($smallWidth) { 6 } else { 7 }))
            } elseif ($control -eq $script:ShortcutPreviewLabel) {
                $control.Height = 26
                $control.Font = New-Object System.Drawing.Font("Segoe UI", 6)
            } elseif ($control.Text -like "SPREAD*") {
                $control.Height = 24
                $control.Font = New-Object System.Drawing.Font("Segoe UI", 7)
            } elseif ($control.Text -like "RIESGO SL*") {
                $control.Height = 14
                $control.Font = New-Object System.Drawing.Font("Segoe UI", 7, [System.Drawing.FontStyle]::Bold)
            } elseif ($control -eq $script:RiskPreviewValue) {
                $control.Height = 28
                $control.Font = New-Object System.Drawing.Font("Segoe UI", 7)
            } elseif ($control.Text -like "VISTA PREVIA*") {
                $control.Height = 14
                $control.Font = New-Object System.Drawing.Font("Segoe UI", 7, [System.Drawing.FontStyle]::Bold)
            } else {
                $control.Height = 14
                $control.Font = New-Object System.Drawing.Font("Segoe UI", 7, $control.Font.Style)
            }
        }
    }
}
$script:Panel.Add_Resize({ Set-PreviewResponsive })

$script:Panel.Add_FormClosing({
    if ($script:TaskbarHwnd -ne [IntPtr]::Zero) {
        [void][OroPreviewNative]::ShowWindow($script:TaskbarHwnd, 5)
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 50
$timer.Add_Tick({
    $now = Get-Date
    $script:DateTimeLabel.Text = $now.ToString("dd/MM/yyyy`nHH:mm:ss")
    $ctrl = [OroPreviewNative]::IsDown(0x11)
    $alt = [OroPreviewNative]::IsDown(0x12)
    $toggleDown = $ctrl -and $alt -and [OroPreviewNative]::IsDown(0x47)
    $quitDown = $ctrl -and $alt -and [OroPreviewNative]::IsDown(0x51)
    $escDown = [OroPreviewNative]::IsDown(0x1B)

    if ($toggleDown -and -not $script:ToggleWasDown) {
        if ($script:PanelVisible) {
            $script:Panel.Hide()
            if ($script:TaskbarHwnd -ne [IntPtr]::Zero) { [void][OroPreviewNative]::ShowWindow($script:TaskbarHwnd, 5) }
            $script:PanelVisible = $false
        } else {
            $rect = New-Object OroRect
            if ($script:TaskbarHwnd -ne [IntPtr]::Zero -and [OroPreviewNative]::GetWindowRect($script:TaskbarHwnd, [ref]$rect)) {
                $width = $rect.Right - $rect.Left
                $height = $rect.Bottom - $rect.Top
                if ($width -ge 40 -and $height -ge 40) {
                    $script:Panel.Bounds = New-Object System.Drawing.Rectangle($rect.Left, $rect.Top, $width, $height)
                    Set-PreviewResponsive
                    [void][OroPreviewNative]::ShowWindow($script:TaskbarHwnd, 0)
                    $script:Panel.Show()
                    $script:Panel.Activate()
                    $script:PanelVisible = $true
                }
            }
        }
    }
    if ($escDown -and -not $script:EscWasDown -and $script:PanelVisible) {
        $script:Panel.Hide()
        if ($script:TaskbarHwnd -ne [IntPtr]::Zero) { [void][OroPreviewNative]::ShowWindow($script:TaskbarHwnd, 5) }
        $script:PanelVisible = $false
    }
    if ($quitDown -and -not $script:QuitWasDown) {
        $timer.Stop()
        $script:Panel.Close()
        [System.Windows.Forms.Application]::ExitThread()
    }
    $script:ToggleWasDown = $toggleDown
    $script:QuitWasDown = $quitDown
    $script:EscWasDown = $escDown
})

$timer.Start()
if ($script:TaskbarHwnd -ne [IntPtr]::Zero) {
    $initialRect = New-Object OroRect
    if ([OroPreviewNative]::GetWindowRect($script:TaskbarHwnd, [ref]$initialRect)) {
        $initialWidth = $initialRect.Right - $initialRect.Left
        $initialHeight = $initialRect.Bottom - $initialRect.Top
        if ($initialWidth -ge 40 -and $initialHeight -ge 40) {
            $script:Panel.Bounds = New-Object System.Drawing.Rectangle($initialRect.Left, $initialRect.Top, $initialWidth, $initialHeight)
            Set-PreviewResponsive
            [void][OroPreviewNative]::ShowWindow($script:TaskbarHwnd, 0)
            $script:Panel.Show()
            $script:Panel.Activate()
            $script:PanelVisible = $true
        }
    }
}
[System.Windows.Forms.Application]::Run()
