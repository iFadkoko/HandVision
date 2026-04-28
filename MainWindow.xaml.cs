using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace HandVision;

/// <summary>
/// MainWindow — C# WPF client for Python OpenCV hand tracking engine.
/// Connects via TCP socket to receive JPEG frames + JSON landmark data.
/// </summary>
public partial class MainWindow : Window
{
    // ─── Connection ───
    private TcpClient? _tcpClient;
    private NetworkStream? _stream;
    private Process? _pythonProcess;
    private CancellationTokenSource? _cts;
    private bool _isConnected;
    private int _frameCount;
    
    // ─── Brushes ───
    private static readonly SolidColorBrush AccentBrush = new(Color.FromRgb(0x00, 0xE5, 0xA0));
    private static readonly SolidColorBrush ErrorBrush = new(Color.FromRgb(0xFF, 0x47, 0x57));
    private static readonly SolidColorBrush SuccessBrush = new(Color.FromRgb(0x2E, 0xD5, 0x73));
    private static readonly SolidColorBrush WarningBrush = new(Color.FromRgb(0xFF, 0xB8, 0x00));
    private static readonly SolidColorBrush PrimaryBrush = new(Color.FromRgb(0x6C, 0x63, 0xFF));
    private static readonly SolidColorBrush MutedBrush = new(Color.FromRgb(0x60, 0x60, 0x70));
    private static readonly SolidColorBrush FingerUpBg = new(Color.FromRgb(0x1A, 0x3A, 0x2A));
    private static readonly SolidColorBrush FingerUpBorder = new(Color.FromRgb(0x00, 0xE5, 0xA0));
    private static readonly SolidColorBrush FingerDownBg = new(Color.FromRgb(0x22, 0x23, 0x2B));
    private static readonly SolidColorBrush FingerDownBorder = new(Color.FromRgb(0x2A, 0x2B, 0x35));
    
    // ─── Panel state ───
    private bool _leftPanelVisible = true;
    private bool _rightPanelVisible = true;

    public MainWindow()
    {
        InitializeComponent();
    }

    private void Window_Loaded(object sender, RoutedEventArgs e)
    {
        AppendLog("HandVision initialized");
        AppendLog("Architecture: C# WPF ← TCP → Python OpenCV");
        AppendLog("Ready to connect to Python engine");
    }

    private void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        StopEngine();
    }

    // ═══════════════════════════════════════════
    // ENGINE CONTROL
    // ═══════════════════════════════════════════

    private async void BtnConnect_Click(object sender, RoutedEventArgs e)
    {
        await StartEngine();
    }

    private void BtnDisconnect_Click(object sender, RoutedEventArgs e)
    {
        StopEngine();
    }

    private async Task StartEngine()
    {
        BtnConnect.IsEnabled = false;
        BtnDisconnect.IsEnabled = true;
        
        UpdateStatus("Starting Python engine...", WarningBrush);
        AppendLog("Starting Python hand tracking engine...");
        
        // Find embedded Python
        var pythonExe = FindEmbeddedPython();
        if (pythonExe == null)
        {
            AppendLog("[ERROR] Cannot find embedded Python runtime");
            AppendLog("[INFO]  Expected at: <app>/python/python.exe");
            AppendLog("[INFO]  Run build_release.ps1 to setup embedded Python");
            UpdateStatus("Error: Python not found", ErrorBrush);
            BtnConnect.IsEnabled = true;
            BtnDisconnect.IsEnabled = false;
            return;
        }
        
        var scriptPath = FindPythonScript();
        if (scriptPath == null)
        {
            AppendLog("[ERROR] Cannot find hand_tracker.py");
            UpdateStatus("Error: Script not found", ErrorBrush);
            BtnConnect.IsEnabled = true;
            BtnDisconnect.IsEnabled = false;
            return;
        }
        
        AppendLog($"Python: {pythonExe}");
        AppendLog($"Script: {scriptPath}");
        
        // Start Python process using embedded Python directly
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = pythonExe,
                Arguments = $"\"{scriptPath}\"",
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(scriptPath) ?? ".",
            };
            
            _pythonProcess = new Process { StartInfo = psi };
            _pythonProcess.OutputDataReceived += (s, args) =>
            {
                if (args.Data != null)
                    Dispatcher.BeginInvoke(() => AppendLog($"[PY] {args.Data}"));
            };
            _pythonProcess.ErrorDataReceived += (s, args) =>
            {
                if (args.Data != null)
                    Dispatcher.BeginInvoke(() => AppendLog($"[PY:ERR] {args.Data}"));
            };
            
            _pythonProcess.Start();
            _pythonProcess.BeginOutputReadLine();
            _pythonProcess.BeginErrorReadLine();
            
            AppendLog($"Python process started (PID: {_pythonProcess.Id})");
            
            // Wait a moment for Python server to start
            await Task.Delay(2500);
            
            // Connect via TCP
            await ConnectToEngine();
        }
        catch (Exception ex)
        {
            AppendLog($"[ERROR] Failed to start engine: {ex.Message}");
            UpdateStatus("Error starting engine", ErrorBrush);
            BtnConnect.IsEnabled = true;
            BtnDisconnect.IsEnabled = false;
        }
    }

    private async Task ConnectToEngine()
    {
        const int maxRetries = 5;
        
        for (int i = 0; i < maxRetries; i++)
        {
            try
            {
                AppendLog($"Connecting to TCP 127.0.0.1:9876 (attempt {i + 1}/{maxRetries})...");
                
                _tcpClient = new TcpClient();
                await _tcpClient.ConnectAsync("127.0.0.1", 9876);
                _stream = _tcpClient.GetStream();
                _isConnected = true;
                
                UpdateStatus("CONNECTED — Tracking Active", AccentBrush);
                AppendLog("✓ Connected to Python engine!");
                
                PlaceholderPanel.Visibility = Visibility.Collapsed;
                
                // Start receiving frames
                _cts = new CancellationTokenSource();
                _ = Task.Run(() => ReceiveLoop(_cts.Token));
                
                return;
            }
            catch (Exception)
            {
                await Task.Delay(1000);
            }
        }
        
        AppendLog("[ERROR] Could not connect to Python engine after retries");
        UpdateStatus("Connection failed", ErrorBrush);
        BtnConnect.IsEnabled = true;
        BtnDisconnect.IsEnabled = false;
    }

    private void StopEngine()
    {
        _isConnected = false;
        _cts?.Cancel();
        
        // Send quit command
        try
        {
            if (_stream != null && _tcpClient?.Connected == true)
            {
                var quitBytes = Encoding.UTF8.GetBytes("QUIT\n");
                _stream.Write(quitBytes, 0, quitBytes.Length);
            }
        }
        catch { /* ignore */ }
        
        _stream?.Dispose();
        _tcpClient?.Dispose();
        
        // Kill Python process
        try
        {
            if (_pythonProcess != null && !_pythonProcess.HasExited)
            {
                _pythonProcess.Kill(entireProcessTree: true);
                _pythonProcess.WaitForExit(3000);
            }
        }
        catch { /* ignore */ }
        _pythonProcess?.Dispose();
        _pythonProcess = null;
        
        _frameCount = 0;
        
        Dispatcher.BeginInvoke(() =>
        {
            UpdateStatus("Disconnected", MutedBrush);
            AppendLog("Engine stopped");
            
            PlaceholderPanel.Visibility = Visibility.Visible;
            CameraImage.Source = null;
            
            HandCountText.Text = "0";
            Hand1Panel.Visibility = Visibility.Collapsed;
            Hand2Panel.Visibility = Visibility.Collapsed;
            
            BtnConnect.IsEnabled = true;
            BtnDisconnect.IsEnabled = false;
            
            FrameCountText.Text = "Frames: 0";
            HandsStatusText.Text = "Hands: 0";
            FpsText.Text = "FPS: —";
        });
    }

    // ═══════════════════════════════════════════
    // FRAME RECEIVE LOOP
    // ═══════════════════════════════════════════

    private async Task ReceiveLoop(CancellationToken ct)
    {
        var headerBuf = new byte[8];
        
        while (!ct.IsCancellationRequested && _isConnected)
        {
            try
            {
                // Read header: [JPEG_SIZE:4][JSON_SIZE:4]
                if (!await ReadExactAsync(_stream!, headerBuf, 0, 8, ct))
                    break;
                
                int jpegSize = ReadInt32BE(headerBuf, 0);
                int jsonSize = ReadInt32BE(headerBuf, 4);
                
                if (jpegSize <= 0 || jpegSize > 5_000_000 || jsonSize <= 0 || jsonSize > 500_000)
                {
                    AppendLogSafe("[WARN] Invalid frame size, skipping");
                    break;
                }
                
                // Read JPEG data
                var jpegData = new byte[jpegSize];
                if (!await ReadExactAsync(_stream!, jpegData, 0, jpegSize, ct))
                    break;
                
                // Read JSON data
                var jsonData = new byte[jsonSize];
                if (!await ReadExactAsync(_stream!, jsonData, 0, jsonSize, ct))
                    break;
                
                // Parse and display
                _frameCount++;
                var jsonStr = Encoding.UTF8.GetString(jsonData);
                
                Dispatcher.BeginInvoke(() =>
                {
                    DisplayFrame(jpegData);
                    ProcessLandmarkData(jsonStr);
                });
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                AppendLogSafe($"[ERROR] Receive: {ex.Message}");
                break;
            }
        }
        
        if (_isConnected)
        {
            _isConnected = false;
            Dispatcher.BeginInvoke(() =>
            {
                UpdateStatus("Connection lost", ErrorBrush);
                AppendLog("Connection to Python engine lost");
                PlaceholderPanel.Visibility = Visibility.Visible;
                BtnConnect.IsEnabled = true;
                BtnDisconnect.IsEnabled = false;
            });
        }
    }

    private static async Task<bool> ReadExactAsync(NetworkStream stream, byte[] buffer, int offset, int count, CancellationToken ct)
    {
        int totalRead = 0;
        while (totalRead < count)
        {
            int read = await stream.ReadAsync(buffer.AsMemory(offset + totalRead, count - totalRead), ct);
            if (read == 0) return false;
            totalRead += read;
        }
        return true;
    }

    private static int ReadInt32BE(byte[] buffer, int offset)
    {
        return (buffer[offset] << 24)
             | (buffer[offset + 1] << 16)
             | (buffer[offset + 2] << 8)
             | buffer[offset + 3];
    }

    // ═══════════════════════════════════════════
    // DISPLAY
    // ═══════════════════════════════════════════

    private void DisplayFrame(byte[] jpegData)
    {
        try
        {
            var bitmap = new BitmapImage();
            using var ms = new MemoryStream(jpegData);
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.StreamSource = ms;
            bitmap.EndInit();
            bitmap.Freeze();
            
            CameraImage.Source = bitmap;
            PlaceholderPanel.Visibility = Visibility.Collapsed;
        }
        catch
        {
            // Skip bad frame
        }
    }

    private void ProcessLandmarkData(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            
            // FPS
            if (root.TryGetProperty("fps", out var fpsEl))
            {
                var fps = fpsEl.GetDouble();
                FpsText.Text = $"FPS: {fps:F0}";
            }
            
            // Frame count
            FrameCountText.Text = $"Frames: {_frameCount}";
            
            // Hands
            if (root.TryGetProperty("hand_count", out var hcEl))
            {
                var handCount = hcEl.GetInt32();
                HandCountText.Text = handCount.ToString();
                HandsStatusText.Text = $"Hands: {handCount}";
                
                Hand1Panel.Visibility = handCount >= 1 ? Visibility.Visible : Visibility.Collapsed;
                Hand2Panel.Visibility = handCount >= 2 ? Visibility.Visible : Visibility.Collapsed;
            }
            
            if (root.TryGetProperty("hands", out var handsEl))
            {
                int idx = 0;
                foreach (var hand in handsEl.EnumerateArray())
                {
                    if (idx == 0) UpdateHandPanel(hand, 1);
                    else if (idx == 1) UpdateHandPanel(hand, 2);
                    idx++;
                }
            }
        }
        catch
        {
            // Skip bad JSON
        }
    }

    private void UpdateHandPanel(JsonElement hand, int handNum)
    {
        var gesture = hand.GetProperty("gesture").GetString() ?? "—";
        var handedness = hand.GetProperty("handedness").GetString() ?? "?";
        var confidence = hand.GetProperty("confidence").GetDouble();
        var fingerCount = hand.GetProperty("finger_count").GetInt32();
        
        // Get finger states
        bool[] fingers = [false, false, false, false, false];
        if (hand.TryGetProperty("fingers_up", out var fingersEl))
        {
            int fi = 0;
            foreach (var f in fingersEl.EnumerateArray())
            {
                if (fi < 5) fingers[fi] = f.GetBoolean();
                fi++;
            }
        }
        
        if (handNum == 1)
        {
            Hand1Side.Text = handedness;
            Hand1Gesture.Text = gesture;
            Hand1Fingers.Text = $"Fingers up: {fingerCount}";
            Hand1Confidence.Text = $"Confidence: {confidence:P0}";
            
            UpdateFingerIndicator(H1Thumb, fingers[0]);
            UpdateFingerIndicator(H1Index, fingers[1]);
            UpdateFingerIndicator(H1Middle, fingers[2]);
            UpdateFingerIndicator(H1Ring, fingers[3]);
            UpdateFingerIndicator(H1Pinky, fingers[4]);
        }
        else
        {
            Hand2Side.Text = handedness;
            Hand2Gesture.Text = gesture;
            Hand2Fingers.Text = $"Fingers up: {fingerCount}";
            Hand2Confidence.Text = $"Confidence: {confidence:P0}";
            
            UpdateFingerIndicator(H2Thumb, fingers[0]);
            UpdateFingerIndicator(H2Index, fingers[1]);
            UpdateFingerIndicator(H2Middle, fingers[2]);
            UpdateFingerIndicator(H2Ring, fingers[3]);
            UpdateFingerIndicator(H2Pinky, fingers[4]);
        }
    }

    private void UpdateFingerIndicator(Border indicator, bool isUp)
    {
        indicator.Background = isUp ? FingerUpBg : FingerDownBg;
        indicator.BorderBrush = isUp ? FingerUpBorder : FingerDownBorder;
        
        if (indicator.Child is TextBlock tb)
        {
            tb.Foreground = isUp ? AccentBrush : MutedBrush;
        }
    }

    // ═══════════════════════════════════════════
    // COMMANDS TO PYTHON
    // ═══════════════════════════════════════════

    private void SendCommand(string command)
    {
        try
        {
            if (_stream != null && _tcpClient?.Connected == true)
            {
                var bytes = Encoding.UTF8.GetBytes(command + "\n");
                _stream.Write(bytes, 0, bytes.Length);
                AppendLog($"[CMD] Sent: {command}");
            }
        }
        catch (Exception ex)
        {
            AppendLog($"[CMD] Failed: {ex.Message}");
        }
    }

    // ═══════════════════════════════════════════
    // UI EVENT HANDLERS
    // ═══════════════════════════════════════════

    private void TglLandmarks_Click(object sender, RoutedEventArgs e) => SendCommand("TOGGLE_LANDMARKS");
    private void TglSkeleton_Click(object sender, RoutedEventArgs e) => SendCommand("TOGGLE_SKELETON");
    private void TglBoundingBox_Click(object sender, RoutedEventArgs e) => SendCommand("TOGGLE_BBOX");
    private void TglMirror_Click(object sender, RoutedEventArgs e) => SendCommand("TOGGLE_MIRROR");
    
    private void SliderConfidence_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (LblConfidence == null) return;
        var val = e.NewValue / 100.0;
        LblConfidence.Text = val.ToString("F2");
        
        if (_isConnected)
            SendCommand($"SET_CONFIDENCE:{val:F2}");
    }

    // ═══════════════════════════════════════════
    // COLOR SETTINGS HANDLERS
    // ═══════════════════════════════════════════

    private void SliderWbTemp_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (LblWbTemp == null) return;
        var val = (int)e.NewValue;
        LblWbTemp.Text = val.ToString();
        
        if (_isConnected)
            SendCommand($"SET_WB_TEMP:{val}");
    }

    private void TglAutoWb_Click(object sender, RoutedEventArgs e) => SendCommand("TOGGLE_AUTO_WB");

    private void SliderBrightness_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (LblBrightness == null) return;
        var val = (int)e.NewValue;
        LblBrightness.Text = val.ToString();
        
        if (_isConnected)
            SendCommand($"SET_BRIGHTNESS:{val}");
    }

    private void SliderContrast_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (LblContrast == null) return;
        var val = (int)e.NewValue;
        LblContrast.Text = val.ToString();
        
        if (_isConnected)
            SendCommand($"SET_CONTRAST:{val}");
    }

    private void SliderSaturation_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (LblSaturation == null) return;
        var val = (int)e.NewValue;
        LblSaturation.Text = val.ToString();
        
        if (_isConnected)
            SendCommand($"SET_SATURATION:{val}");
    }

    private void SliderHue_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (LblHue == null) return;
        var val = (int)e.NewValue;
        LblHue.Text = val.ToString();
        
        if (_isConnected)
            SendCommand($"SET_HUE:{val}");
    }

    private void SliderGamma_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (LblGamma == null) return;
        // Slider range 10-300 → gamma 0.10-3.00
        var gamma = e.NewValue / 100.0;
        LblGamma.Text = gamma.ToString("F2");
        
        if (_isConnected)
            SendCommand($"SET_GAMMA:{gamma:F2}");
    }

    private void BtnResetColor_Click(object sender, RoutedEventArgs e)
    {
        // Reset all sliders to defaults
        SliderWbTemp.Value = 6500;
        SliderBrightness.Value = 0;
        SliderContrast.Value = 0;
        SliderSaturation.Value = 0;
        SliderHue.Value = 0;
        SliderGamma.Value = 100;
        TglAutoWb.IsChecked = false;
        
        if (_isConnected)
            SendCommand("RESET_COLOR");
        
        AppendLog("Color settings reset to defaults");
    }

    private void BtnToggleLeftPanel_Click(object sender, RoutedEventArgs e)
    {
        _leftPanelVisible = !_leftPanelVisible;
        LeftPanel.Visibility = _leftPanelVisible ? Visibility.Visible : Visibility.Collapsed;
        LeftPanelCol.Width = _leftPanelVisible ? new GridLength(240) : new GridLength(0);
    }

    private void BtnToggleRightPanel_Click(object sender, RoutedEventArgs e)
    {
        _rightPanelVisible = !_rightPanelVisible;
        RightPanel.Visibility = _rightPanelVisible ? Visibility.Visible : Visibility.Collapsed;
        RightPanelCol.Width = _rightPanelVisible ? new GridLength(260) : new GridLength(0);
    }

    // ═══════════════════════════════════════════
    // UTILITIES
    // ═══════════════════════════════════════════

    private void UpdateStatus(string text, SolidColorBrush color)
    {
        StatusText.Text = text.ToUpperInvariant();
        StatusDot.Fill = color;
        StatusBarText.Text = text;
        StatusBarDot.Fill = color;
        
        EngineStatus.Text = _isConnected ? "● Online" : "● Offline";
        EngineStatus.Foreground = _isConnected ? SuccessBrush : ErrorBrush;
    }

    private void AppendLog(string message)
    {
        var timestamp = DateTime.Now.ToString("HH:mm:ss");
        LogText.Text += $"[{timestamp}] {message}\n";
        LogScroller.ScrollToEnd();
    }

    private void AppendLogSafe(string message)
    {
        Dispatcher.BeginInvoke(() => AppendLog(message));
    }

    private static string? FindEmbeddedPython()
    {
        var baseDir = AppDomain.CurrentDomain.BaseDirectory;
        
        var candidates = new[]
        {
            // Distribution: python/ next to .exe
            Path.Combine(baseDir, "python", "python.exe"),
            // Dev: project root from bin/Debug/net9.0-windows/
            Path.Combine(baseDir, "..", "..", "..", "python", "python.exe"),
            // Dev: project root from bin/Debug/net9.0-windows/win-x64/
            Path.Combine(baseDir, "..", "..", "..", "..", "python", "python.exe"),
            // CWD fallback
            Path.Combine(Directory.GetCurrentDirectory(), "python", "python.exe"),
        };
        
        foreach (var p in candidates)
        {
            var full = Path.GetFullPath(p);
            if (File.Exists(full)) return full;
        }
        
        return null;
    }

    private static string? FindPythonScript()
    {
        var baseDir = AppDomain.CurrentDomain.BaseDirectory;
        
        var candidates = new[]
        {
            // Distribution: next to .exe
            Path.Combine(baseDir, "hand_tracker.py"),
            // Dev: project root from bin/Debug/net9.0-windows/
            Path.Combine(baseDir, "..", "..", "..", "hand_tracker.py"),
            // Dev: project root from bin/Debug/net9.0-windows/win-x64/
            Path.Combine(baseDir, "..", "..", "..", "..", "hand_tracker.py"),
            // CWD fallback
            Path.Combine(Directory.GetCurrentDirectory(), "hand_tracker.py"),
        };
        
        foreach (var p in candidates)
        {
            var full = Path.GetFullPath(p);
            if (File.Exists(full)) return full;
        }
        
        return null;
    }
}