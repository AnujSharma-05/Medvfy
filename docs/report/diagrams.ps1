# Draws the report's flowchart and pin diagram (PNG) with System.Drawing.
Add-Type -AssemblyName System.Drawing
$out = Split-Path -Parent $MyInvocation.MyCommand.Path

function New-Canvas($w, $h) {
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'; $g.TextRenderingHint = 'AntiAliasGridFit'
    $g.Clear([System.Drawing.Color]::White)
    return @($bmp, $g)
}
$font = New-Object System.Drawing.Font 'Times New Roman', 22
$bold = New-Object System.Drawing.Font 'Times New Roman', 23, ([System.Drawing.FontStyle]::Bold)
$small = New-Object System.Drawing.Font 'Times New Roman', 19, ([System.Drawing.FontStyle]::Italic)
$pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(40, 40, 40)), 3
$arrow = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(40, 40, 40)), 3
$arrow.CustomEndCap = New-Object System.Drawing.Drawing2D.AdjustableArrowCap 6, 6
$black = [System.Drawing.Brushes]::Black
$center = New-Object System.Drawing.StringFormat; $center.Alignment = 'Center'; $center.LineAlignment = 'Center'
function C($hex) { [System.Drawing.ColorTranslator]::FromHtml($hex) }

function Box($g, $x, $y, $w, $h, $fill, $title, $text) {
    $g.FillRectangle((New-Object System.Drawing.SolidBrush (C $fill)), $x, $y, $w, $h)
    $g.DrawRectangle($pen, $x, $y, $w, $h)
    if ($text) {
        $g.DrawString($title, $bold, $black, (New-Object System.Drawing.RectangleF ($x + 8), ($y + 6), ($w - 16), 34), $center)
        $g.DrawString($text, $font, $black, (New-Object System.Drawing.RectangleF ($x + 10), ($y + 40), ($w - 20), ($h - 46)), $center)
    } else {
        $g.DrawString($title, $bold, $black, (New-Object System.Drawing.RectangleF ($x + 8), $y, ($w - 16), $h), $center)
    }
}
function Diamond($g, $cx, $cy, $w, $h, $fill, $text) {
    $pts = [System.Drawing.PointF[]]@((New-Object System.Drawing.PointF $cx, ($cy - $h / 2)), (New-Object System.Drawing.PointF ($cx + $w / 2), $cy),
        (New-Object System.Drawing.PointF $cx, ($cy + $h / 2)), (New-Object System.Drawing.PointF ($cx - $w / 2), $cy))
    $g.FillPolygon((New-Object System.Drawing.SolidBrush (C $fill)), $pts); $g.DrawPolygon($pen, $pts)
    $g.DrawString($text, $bold, $black, (New-Object System.Drawing.RectangleF ($cx - $w / 2 + 40), ($cy - $h / 2), ($w - 80), $h), $center)
}
function Arrow($g, $x1, $y1, $x2, $y2) { $g.DrawLine($arrow, $x1, $y1, $x2, $y2) }
function Label($g, $x, $y, $text) { $g.DrawString($text, $small, $black, $x, $y) }

# ---------------------------------------------------------------- flowchart
$c = New-Canvas 1600 2050; $bmp = $c[0]; $g = $c[1]
$g.DrawString("MedVerify: complete working", (New-Object System.Drawing.Font 'Times New Roman', 30, ([System.Drawing.FontStyle]::Bold)), $black,
    (New-Object System.Drawing.RectangleF 0, 10, 1600, 50), $center)

Box $g 40 80 480 200 '#E2ECF8' 'ADMIN MODE (console)' "Start server, camera, pill box`nCamera set-up: find drawn squares,`nempty photo, loaded photo + counts`nBox tests, live status of A-D"
Box $g 560 80 480 200 '#E6F4EA' 'CARETAKER MODE (app)' "Password login`nSet schedule (time, box, pills)`nSend reminder, recalibrate`nSee dose history and alerts"
Box $g 1080 80 480 200 '#FFF4E0' 'PATIENT MODE (screen)' "Large English + Hindi text`n'Take N pills from box X'`nStop-sound button`nAlert banners with OK"

Box $g 300 340 1000 120 '#F2F2F2' 'Flask server: schedule + dose manager' "Every few seconds: is a dose due now? (or a manual dose from the caretaker)"
Arrow $g 280 280 500 340; Arrow $g 800 280 800 340
Arrow $g 1320 280 1100 340

Box $g 300 520 1000 120 '#FFF4E0' 'Dose opens (30-min window)' "ESP32: LED of box X ON + buzzer beeps  |  Screen: 'Take N from box X'"
Arrow $g 800 460 800 520
Arrow $g 800 640 800 700

Box $g 300 700 1000 140 '#E2ECF8' 'Camera service (every 0.5 s)' "Scene still? -> measure medicine area inside each drawn square`ncount = area / area of one pill  |  hand crossing a pen line = view blocked"
Arrow $g 800 840 800 900

Diamond $g 800 1010 760 220 '#FDECEA' 'Pill count change confirmed (2 readings, 2 s)?'
Label $g 1190 955 'no change in 30 min'
Arrow $g 1180 1010 1430 1010; Box $g 1300 1060 280 130 '#F2F2F2' 'MISSED' "Caretaker alerted,`ndose closed"
Arrow $g 1430 1010 1430 1060
Label $g 815 1125 'yes'
Arrow $g 800 1120 800 1180

# outcome row
$y = 1180; $h = 260; $w = 290
Box $g 20 ($y + 80) $w $h '#E6F4EA' 'Correct dose' "LED OFF, buzzer stops`nPatient: 'Done'`nCaretaker: dose taken"
Box $g 330 ($y + 80) $w $h '#FDECEA' 'Too many pills' "ALARM siren (3 s)`nRed 'Stop! put the`nextra back'`nCaretaker: CRITICAL"
Box $g 640 ($y + 80) $w $h '#FFF4E0' 'Wrong box' "Screen: 'Not D, take`nfrom box A'`nLED re-points to A`nand beeps again"
Box $g 950 ($y + 80) $w $h '#FFF4E0' '2+ boxes at once' "WARN double beep`n'Take one medicine`nat a time'`nCaretaker informed"
Box $g 1260 ($y + 80) 320 $h '#F2F2F2' 'Other cases' "Fewer pills: PARTIAL`nPut back: PUT_BACK`nNo dose due: UNSCHEDULED`nFew left: LOW_STOCK"
$g.DrawLine($pen, 165, $y + 30, 1420, $y + 30); $g.DrawLine($pen, 800, $y, 800, $y + 30)
foreach ($x in 165, 475, 785, 1095, 1420) { Arrow $g $x ($y + 30) $x ($y + 80) }

Box $g 300 1610 1000 120 '#E6F4EA' 'Notifications' "Alert log -> caretaker app + admin console  |  patient screen shows the result"
foreach ($x in 165, 475, 785, 1095, 1420) { $g.DrawLine($pen, $x, 1520, $x, 1570) }
$g.DrawLine($pen, 165, 1570, 1420, 1570); Arrow $g 800 1570 800 1610

Box $g 300 1790 1000 150 '#F2F2F2' 'Safety guards (always on)' "Moving hands are never judged (scene must be still)`nAll boxes blocked > 10 s = camera/sheet moved -> CRITICAL alert`nThe camera sees removal only; it never claims the pill was swallowed"
Arrow $g 800 1730 800 1790
$bmp.Save("$out\flowchart.png", [System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose()

# ---------------------------------------------------------------- pin diagram
$c = New-Canvas 1600 820; $bmp = $c[0]; $g = $c[1]
$g.DrawString("ESP32 pin connections", (New-Object System.Drawing.Font 'Times New Roman', 30, ([System.Drawing.FontStyle]::Bold)), $black,
    (New-Object System.Drawing.RectangleF 0, 10, 1600, 50), $center)
# ESP32 board
$g.FillRectangle((New-Object System.Drawing.SolidBrush (C '#2E3B4E')), 80, 110, 420, 640)
$white = [System.Drawing.Brushes]::White
$g.DrawString("ESP32 DevKit`n(ESP32-D0WD-V3)", $bold, $white, (New-Object System.Drawing.RectangleF 80, 130, 420, 80), $center)
$g.DrawString("USB (CP210x)`n115200 baud", $font, $white, (New-Object System.Drawing.RectangleF 80, 650, 420, 80), $center)
$pins = @(@('GPIO16', 'LED A', '#F4C430', 'A'), @('GPIO17', 'LED B', '#2F6FDE', 'B'), @('GPIO18', 'LED C', '#E03C31', 'C'),
          @('GPIO19', 'LED D', '#5FBF5F', 'D'), @('GPIO25', 'Buzzer S', '#333333', ''), @('3V3', 'Buzzer +', '#C0392B', ''),
          @('GND', 'Common GND', '#000000', ''), @('GPIO2', 'On-board LED', '#888888', ''))
$i = 0
foreach ($p in $pins) {
    $py = 250 + $i * 50
    $g.FillRectangle([System.Drawing.Brushes]::Gold, 480, $py - 8, 20, 16)
    $g.DrawString($p[0], $font, $white, 330, $py - 16)
    if ($p[0] -ne 'GPIO2') {
        $wire = New-Object System.Drawing.Pen (C $p[2]), 5
        $g.DrawLine($wire, 500, $py, 820, $py)
    }
    $i++
}
# LEDs with resistors to GND
for ($k = 0; $k -lt 4; $k++) {
    $py = 250 + $k * 50
    $g.FillRectangle([System.Drawing.Brushes]::Tan, 820, $py - 9, 90, 18); $g.DrawRectangle($pen, 820, $py - 9, 90, 18)
    $g.DrawString('220-330 ohm', $small, $black, 820, $py - 34)
    $g.DrawLine($pen, 910, $py, 1000, $py)
    $g.FillEllipse((New-Object System.Drawing.SolidBrush (C $pins[$k][2])), 1000, $py - 18, 36, 36); $g.DrawEllipse($pen, 1000, $py - 18, 36, 36)
    $g.DrawString("LED $($pins[$k][3])  (compartment $($pins[$k][3]))", $font, $black, 1050, $py - 16)
    $g.DrawLine($pen, 1018, $py + 18, 1018, $py + 22)
}
# buzzer
$g.FillEllipse((New-Object System.Drawing.SolidBrush (C '#222222')), 1000, 440, 90, 90)
$g.DrawString("Buzzer module`nS = GPIO25, + = 3V3, - = GND", $font, $black, 1110, 455)
$g.DrawLine($pen, 820, 450, 1000, 470); $g.DrawLine($pen, 820, 500, 1000, 490)
$g.DrawLine($pen, 820, 550, 1000, 520)
$g.DrawString("LED cathodes (-) and buzzer (-) go to common GND.  On-board LED (GPIO2): boot / command indicator.", $small, $black, 80, 770)
$bmp.Save("$out\pins.png", [System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose()
"ok"
