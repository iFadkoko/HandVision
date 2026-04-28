"""
Hand Tracking Engine — Python + OpenCV + MediaPipe Tasks API
Communicates with C# WPF app via TCP socket.
Sends: JPEG frame bytes + JSON landmark data

Uses the newer MediaPipe Tasks API (not mp.solutions)
"""

import cv2
import mediapipe as mp
import numpy as np
import socket
import struct
import json
import time
import threading
import os

from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    HandLandmarker,
    HandLandmarkerOptions,
    HandLandmarkerResult,
    RunningMode,
)
from mediapipe import Image as MpImage, ImageFormat

# ─── Hand landmark connections (21-point model) ───
HAND_CONNECTIONS = [
    # Thumb
    (0, 1), (1, 2), (2, 3), (3, 4),
    # Index
    (0, 5), (5, 6), (6, 7), (7, 8),
    # Middle
    (0, 9), (9, 10), (10, 11), (11, 12),
    # Ring
    (0, 13), (13, 14), (14, 15), (15, 16),
    # Pinky
    (0, 17), (17, 18), (18, 19), (19, 20),
    # Palm
    (5, 9), (9, 13), (13, 17),
]


class HandTracker:
    def __init__(self, camera_index=0, max_hands=2, detection_confidence=0.7, tracking_confidence=0.6):
        self.camera_index = camera_index
        self.max_hands = max_hands
        self.detection_confidence = detection_confidence
        self.tracking_confidence = tracking_confidence
        self.cap = None
        self.landmarker = None
        self.running = False
        self.server_socket = None
        self.client_socket = None
        self.port = 9876
        self.frame_count = 0
        self.fps = 0.0
        self.last_fps_time = time.time()
        
        # Latest detection result (used in VIDEO/LIVE_STREAM mode)
        self._latest_result = None
        self._result_lock = threading.Lock()
        
        # Drawing settings
        self.draw_landmarks = True
        self.draw_skeleton = True
        self.draw_bounding_box = True
        self.mirror = True
        
        # ─── Color Correction Settings ───
        self.wb_temp = 6500       # White balance temperature (K): 2000=warm, 6500=daylight, 10000=cool
        self.brightness = 0       # -100 to +100
        self.contrast = 0         # -100 to +100
        self.saturation = 0       # -100 to +100
        self.hue = 0              # -180 to +180 degrees
        self.gamma = 1.0          # 0.1 to 3.0  (1.0 = no change)
        self.auto_wb = False      # Auto white balance via gray-world algorithm
        
        # Colors (BGR)
        self.landmark_color = (0, 255, 170)      # Neon green
        self.skeleton_color = (255, 170, 0)       # Cyan-blue
        self.bbox_color = (170, 0, 255)           # Purple
        self.finger_colors = {
            'thumb': (0, 200, 255),     # Orange
            'index': (0, 255, 100),     # Green
            'middle': (255, 200, 0),    # Cyan
            'ring': (255, 100, 200),    # Pink
            'pinky': (200, 100, 255),   # Violet
        }
        
        # Finger tip and pip indices (MediaPipe 21-point)
        self.finger_tips = [4, 8, 12, 16, 20]
        self.finger_pips = [3, 6, 10, 14, 18]
        self.finger_names = ['thumb', 'index', 'middle', 'ring', 'pinky']

    def _find_model_path(self):
        """Find the hand_landmarker.task model file."""
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_landmarker.task"),
            os.path.join(os.getcwd(), "hand_landmarker.task"),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    @staticmethod
    def _wb_temperature_to_rgb(kelvin):
        """Convert color temperature (Kelvin) to BGR scaling factors.
        Based on Tanner Helland's algorithm."""
        temp = max(1000, min(40000, kelvin)) / 100.0
        
        # Red
        if temp <= 66:
            r = 255
        else:
            r = 329.698727446 * ((temp - 60) ** -0.1332047592)
            r = max(0, min(255, r))
        
        # Green
        if temp <= 66:
            g = 99.4708025861 * np.log(temp) - 161.1195681661
        else:
            g = 288.1221695283 * ((temp - 60) ** -0.0755148492)
        g = max(0, min(255, g))
        
        # Blue
        if temp >= 66:
            b = 255
        elif temp <= 19:
            b = 0
        else:
            b = 138.5177312231 * np.log(temp - 10) - 305.0447927307
            b = max(0, min(255, b))
        
        # Normalize to multipliers relative to daylight (6500K)
        ref_r, ref_g, ref_b = 255, 254.07, 250.99  # approx 6500K values
        return b / ref_b, g / ref_g, r / ref_r  # Return as BGR

    def apply_color_correction(self, frame):
        """Apply color correction pipeline to a frame.
        Order: White Balance → Brightness/Contrast → Saturation → Hue → Gamma
        """
        corrected = frame.copy()
        
        # ─── 1. White Balance ───
        if self.auto_wb:
            # Gray-world assumption: average of each channel should be equal
            avg_b, avg_g, avg_r = cv2.mean(corrected)[:3]
            avg_all = (avg_b + avg_g + avg_r) / 3.0
            if avg_b > 0 and avg_g > 0 and avg_r > 0:
                corrected = corrected.astype(np.float32)
                corrected[:, :, 0] *= avg_all / avg_b  # B
                corrected[:, :, 1] *= avg_all / avg_g  # G
                corrected[:, :, 2] *= avg_all / avg_r  # R
                corrected = np.clip(corrected, 0, 255).astype(np.uint8)
        elif self.wb_temp != 6500:
            # Manual white balance via temperature
            b_mul, g_mul, r_mul = self._wb_temperature_to_rgb(self.wb_temp)
            corrected = corrected.astype(np.float32)
            corrected[:, :, 0] *= b_mul
            corrected[:, :, 1] *= g_mul
            corrected[:, :, 2] *= r_mul
            corrected = np.clip(corrected, 0, 255).astype(np.uint8)
        
        # ─── 2. Brightness & Contrast ───
        if self.brightness != 0 or self.contrast != 0:
            # contrast: alpha factor (1.0 = no change, range ~ 0.5 - 2.0)
            # brightness: beta offset
            alpha = 1.0 + (self.contrast / 100.0)
            beta = self.brightness * 1.5  # scale for visible effect
            corrected = cv2.convertScaleAbs(corrected, alpha=alpha, beta=beta)
        
        # ─── 3. Saturation ───
        if self.saturation != 0:
            hsv = cv2.cvtColor(corrected, cv2.COLOR_BGR2HSV).astype(np.float32)
            sat_factor = 1.0 + (self.saturation / 100.0)
            hsv[:, :, 1] = np.clip(hsv[:, :, 1] * sat_factor, 0, 255)
            corrected = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        
        # ─── 4. Hue Shift ───
        if self.hue != 0:
            hsv = cv2.cvtColor(corrected, cv2.COLOR_BGR2HSV).astype(np.float32)
            # OpenCV hue range is 0-179, shift and wrap
            hsv[:, :, 0] = (hsv[:, :, 0] + self.hue / 2.0) % 180
            corrected = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        
        # ─── 5. Gamma ───
        if self.gamma != 1.0:
            inv_gamma = 1.0 / max(0.1, self.gamma)
            table = np.array([((i / 255.0) ** inv_gamma) * 255
                              for i in range(256)]).astype("uint8")
            corrected = cv2.LUT(corrected, table)
        
        return corrected


    def _init_landmarker(self):
        """Initialize the MediaPipe HandLandmarker using Tasks API."""
        model_path = self._find_model_path()
        if model_path is None:
            print("[ERROR] hand_landmarker.task model file not found!")
            print("[INFO]  Download from: https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task")
            return False
        
        print(f"[MODEL] Loading: {model_path}")
        
        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path),
            running_mode=RunningMode.IMAGE,
            num_hands=self.max_hands,
            min_hand_detection_confidence=self.detection_confidence,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=self.tracking_confidence,
        )
        
        self.landmarker = HandLandmarker.create_from_options(options)
        print("[MODEL] HandLandmarker initialized (IMAGE mode)")
        return True

    def start_server(self):
        """Start TCP socket server for C# client connection."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('127.0.0.1', self.port))
        self.server_socket.listen(1)
        self.server_socket.settimeout(1.0)
        print(f"[SERVER] Listening on 127.0.0.1:{self.port}")
        print(f"[SERVER] Waiting for C# client...")

    def wait_for_client(self):
        """Wait for the C# WPF client to connect."""
        while self.running:
            try:
                self.client_socket, addr = self.server_socket.accept()
                self.client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                print(f"[SERVER] Client connected from {addr}")
                return True
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[SERVER] Accept error: {e}")
                return False
        return False

    def count_fingers(self, landmarks, handedness_label):
        """Count raised fingers based on landmark positions."""
        fingers_up = []
        
        # Thumb — check x position relative to IP joint
        is_right = handedness_label == "Right"
        if is_right:
            fingers_up.append(landmarks[4].x < landmarks[3].x)
        else:
            fingers_up.append(landmarks[4].x > landmarks[3].x)
        
        # Other 4 fingers — tip above PIP means finger up (y inverted: smaller = higher)
        for tip, pip in zip(self.finger_tips[1:], self.finger_pips[1:]):
            fingers_up.append(landmarks[tip].y < landmarks[pip].y)
        
        return fingers_up

    def detect_gesture(self, fingers_up):
        """Detect gesture from finger states."""
        count = sum(fingers_up)
        
        if count == 0:
            return "Fist"
        elif count == 5:
            return "Open Palm"
        elif fingers_up == [False, True, False, False, False]:
            return "Pointing"
        elif fingers_up == [False, True, True, False, False]:
            return "Peace"
        elif fingers_up == [True, False, False, False, True]:
            return "Rock"
        elif fingers_up == [True, True, False, False, False]:
            return "Gun"
        elif fingers_up == [True, False, False, False, False]:
            return "Thumbs Up"
        elif fingers_up == [False, True, True, True, False]:
            return "Three"
        elif fingers_up == [False, True, True, True, True]:
            return "Four"
        elif fingers_up == [False, False, False, False, True]:
            return "Pinky"
        else:
            return f"Fingers: {count}"

    def draw_fancy_landmarks(self, frame, landmarks, handedness_label, fingers_up, gesture):
        """Draw elegant hand tracking visualization."""
        h, w, _ = frame.shape
        
        # Convert normalized landmarks to pixel coordinates
        points = []
        for lm in landmarks:
            px, py = int(lm.x * w), int(lm.y * h)
            points.append((px, py))
        
        if self.draw_skeleton:
            # Finger index ranges for coloring
            finger_conn_ranges = {
                'thumb': [(0, 1), (1, 2), (2, 3), (3, 4)],
                'index': [(0, 5), (5, 6), (6, 7), (7, 8)],
                'middle': [(0, 9), (9, 10), (10, 11), (11, 12)],
                'ring': [(0, 13), (13, 14), (14, 15), (15, 16)],
                'pinky': [(0, 17), (17, 18), (18, 19), (19, 20)],
            }
            
            for start, end in HAND_CONNECTIONS:
                # Determine finger for color
                color = self.skeleton_color
                for fname, conns in finger_conn_ranges.items():
                    if (start, end) in conns:
                        color = self.finger_colors[fname]
                        break
                
                # Main line
                cv2.line(frame, points[start], points[end], color, 2, cv2.LINE_AA)
                # Subtle glow (lighter, thinner)
                glow = tuple(min(255, c + 60) for c in color)
                cv2.line(frame, points[start], points[end], glow, 1, cv2.LINE_AA)
        
        if self.draw_landmarks:
            for i, (px, py) in enumerate(points):
                if i in self.finger_tips:
                    finger_idx = self.finger_tips.index(i)
                    fname = self.finger_names[finger_idx]
                    color = self.finger_colors[fname]
                    radius = 5 if fingers_up[finger_idx] else 3
                    
                    # Outer glow ring
                    cv2.circle(frame, (px, py), radius + 3, color, 1, cv2.LINE_AA)
                    # Filled dot
                    cv2.circle(frame, (px, py), radius, color, -1, cv2.LINE_AA)
                elif i == 0:  # Wrist
                    cv2.circle(frame, (px, py), 5, self.landmark_color, -1, cv2.LINE_AA)
                    cv2.circle(frame, (px, py), 7, self.landmark_color, 1, cv2.LINE_AA)
                else:
                    cv2.circle(frame, (px, py), 2, self.landmark_color, -1, cv2.LINE_AA)
        
        if self.draw_bounding_box:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            margin = 20
            x_min, x_max = max(0, min(xs) - margin), min(w, max(xs) + margin)
            y_min, y_max = max(0, min(ys) - margin), min(h, max(ys) + margin)
            
            # Thin border
            cv2.rectangle(frame, (x_min, y_min), (x_max, y_max), self.bbox_color, 1, cv2.LINE_AA)
            
            # Corner L-accents
            cl = 15
            corners = [
                ((x_min, y_min), (x_min + cl, y_min), (x_min, y_min + cl)),
                ((x_max, y_min), (x_max - cl, y_min), (x_max, y_min + cl)),
                ((x_min, y_max), (x_min + cl, y_max), (x_min, y_max - cl)),
                ((x_max, y_max), (x_max - cl, y_max), (x_max, y_max - cl)),
            ]
            for corner, h_end, v_end in corners:
                cv2.line(frame, corner, h_end, self.bbox_color, 2, cv2.LINE_AA)
                cv2.line(frame, corner, v_end, self.bbox_color, 2, cv2.LINE_AA)
            
            # Label
            label = f"{handedness_label} | {gesture}"
            ts = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0]
            cv2.rectangle(frame, (x_min, y_min - ts[1] - 10), (x_min + ts[0] + 10, y_min), self.bbox_color, -1)
            cv2.putText(frame, label, (x_min + 5, y_min - 5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

    def draw_hud(self, frame, num_hands):
        """Draw heads-up display info."""
        h, w, _ = frame.shape
        
        # Semi-transparent top bar
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (w, 35), (20, 20, 25), -1)
        cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)
        
        # FPS
        cv2.putText(frame, f"FPS: {self.fps:.0f}", (10, 25),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 170), 1, cv2.LINE_AA)
        
        # Hand count
        cv2.putText(frame, f"Hands: {num_hands}", (130, 25),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 0), 1, cv2.LINE_AA)
        
        # Engine label
        cv2.putText(frame, "MediaPipe Tasks", (w - 180, 25),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 100, 120), 1, cv2.LINE_AA)
        
        # Status
        status = "TRACKING" if num_hands > 0 else "SEARCHING"
        status_color = (0, 255, 100) if num_hands > 0 else (100, 100, 100)
        dot_radius = 4
        cv2.circle(frame, (w - 195, 20), dot_radius, status_color, -1, cv2.LINE_AA)

    def send_frame(self, frame, landmarks_data):
        """Send frame + landmark data to C# client via TCP."""
        if self.client_socket is None:
            return False
        
        try:
            _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            jpeg_bytes = jpeg.tobytes()
            
            json_str = json.dumps(landmarks_data)
            json_bytes = json_str.encode('utf-8')
            
            # Protocol: [JPEG_SIZE:4][JSON_SIZE:4][JPEG_DATA][JSON_DATA]
            header = struct.pack('!II', len(jpeg_bytes), len(json_bytes))
            self.client_socket.sendall(header + jpeg_bytes + json_bytes)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            print("[SERVER] Client disconnected")
            self.client_socket = None
            return False

    def receive_commands(self):
        """Background thread to receive commands from C# client."""
        while self.running and self.client_socket:
            try:
                data = self.client_socket.recv(1024)
                if not data:
                    break
                # Handle multiple commands in one recv
                for line in data.decode('utf-8').strip().split('\n'):
                    cmd = line.strip()
                    if cmd:
                        self.handle_command(cmd)
            except (ConnectionResetError, OSError):
                break
        print("[SERVER] Command receiver stopped")

    def handle_command(self, cmd):
        """Handle commands from C# client."""
        parts = cmd.split(':')
        action = parts[0].upper()
        
        if action == "TOGGLE_LANDMARKS":
            self.draw_landmarks = not self.draw_landmarks
            print(f"[CMD] Landmarks: {'ON' if self.draw_landmarks else 'OFF'}")
        elif action == "TOGGLE_SKELETON":
            self.draw_skeleton = not self.draw_skeleton
            print(f"[CMD] Skeleton: {'ON' if self.draw_skeleton else 'OFF'}")
        elif action == "TOGGLE_BBOX":
            self.draw_bounding_box = not self.draw_bounding_box
            print(f"[CMD] BBox: {'ON' if self.draw_bounding_box else 'OFF'}")
        elif action == "TOGGLE_MIRROR":
            self.mirror = not self.mirror
            print(f"[CMD] Mirror: {'ON' if self.mirror else 'OFF'}")
        elif action == "SET_CONFIDENCE" and len(parts) > 1:
            try:
                self.detection_confidence = float(parts[1])
                # Reinit landmarker with new confidence
                if self.landmarker:
                    self.landmarker.close()
                self._init_landmarker()
                print(f"[CMD] Detection confidence: {self.detection_confidence}")
            except ValueError:
                pass
        # ─── Color Correction Commands ───
        elif action == "SET_WB_TEMP" and len(parts) > 1:
            try:
                self.wb_temp = int(float(parts[1]))
                print(f"[CMD] White Balance: {self.wb_temp}K")
            except ValueError:
                pass
        elif action == "SET_BRIGHTNESS" and len(parts) > 1:
            try:
                self.brightness = int(float(parts[1]))
                print(f"[CMD] Brightness: {self.brightness}")
            except ValueError:
                pass
        elif action == "SET_CONTRAST" and len(parts) > 1:
            try:
                self.contrast = int(float(parts[1]))
                print(f"[CMD] Contrast: {self.contrast}")
            except ValueError:
                pass
        elif action == "SET_SATURATION" and len(parts) > 1:
            try:
                self.saturation = int(float(parts[1]))
                print(f"[CMD] Saturation: {self.saturation}")
            except ValueError:
                pass
        elif action == "SET_HUE" and len(parts) > 1:
            try:
                self.hue = int(float(parts[1]))
                print(f"[CMD] Hue: {self.hue}")
            except ValueError:
                pass
        elif action == "SET_GAMMA" and len(parts) > 1:
            try:
                self.gamma = float(parts[1])
                print(f"[CMD] Gamma: {self.gamma:.2f}")
            except ValueError:
                pass
        elif action == "TOGGLE_AUTO_WB":
            self.auto_wb = not self.auto_wb
            print(f"[CMD] Auto WB: {'ON' if self.auto_wb else 'OFF'}")
        elif action == "RESET_COLOR":
            self.wb_temp = 6500
            self.brightness = 0
            self.contrast = 0
            self.saturation = 0
            self.hue = 0
            self.gamma = 1.0
            self.auto_wb = False
            print("[CMD] Color settings reset to defaults")
        elif action == "QUIT":
            self.running = False
            print("[CMD] Quit requested")

    def run(self):
        """Main tracking loop."""
        self.running = True
        
        # Initialize MediaPipe
        if not self._init_landmarker():
            return
        
        # Open camera
        self.cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(self.camera_index)
        
        if not self.cap.isOpened():
            print("[ERROR] Cannot open camera!")
            return
        
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[CAMERA] Opened at {actual_w}x{actual_h}")
        
        # Start TCP server
        self.start_server()
        
        print()
        print("=" * 50)
        print("  HAND TRACKER ENGINE — RUNNING")
        print("  Using: MediaPipe Tasks API (HandLandmarker)")
        print("  Waiting for C# client to connect...")
        print("  Press Ctrl+C to stop")
        print("=" * 50)
        print()
        
        # Wait for client
        if not self.wait_for_client():
            print("[SERVER] No client connected. Exiting.")
            self.cleanup()
            return
        
        # Start command receiver thread
        cmd_thread = threading.Thread(target=self.receive_commands, daemon=True)
        cmd_thread.start()
        
        # Timestamp for MediaPipe
        timestamp_ms = 0
        
        # Main loop
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                continue
            
            if self.mirror:
                frame = cv2.flip(frame, 1)
            
            # Apply color correction (white balance, brightness, contrast, etc.)
            frame = self.apply_color_correction(frame)
            
            # Convert BGR → RGB for MediaPipe
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = MpImage(image_format=ImageFormat.SRGB, data=rgb_frame)
            
            # Run detection (IMAGE mode — synchronous)
            result = self.landmarker.detect(mp_image)
            
            # Build landmark data for C#
            landmarks_data = {
                'hands': [],
                'fps': self.fps,
                'frame': self.frame_count,
                'timestamp': time.time(),
            }
            
            num_hands = len(result.hand_landmarks) if result.hand_landmarks else 0
            
            if result.hand_landmarks:
                for idx in range(num_hands):
                    hand_lms = result.hand_landmarks[idx]
                    
                    # Get handedness
                    handedness_label = "Unknown"
                    confidence = 0.0
                    if result.handedness and idx < len(result.handedness):
                        h_info = result.handedness[idx]
                        if h_info:
                            handedness_label = h_info[0].category_name
                            confidence = h_info[0].score
                    
                    # Count fingers
                    fingers_up = self.count_fingers(hand_lms, handedness_label)
                    gesture = self.detect_gesture(fingers_up)
                    
                    # Draw on frame
                    self.draw_fancy_landmarks(frame, hand_lms, handedness_label, fingers_up, gesture)
                    
                    # Build JSON data
                    hand_data = {
                        'id': idx,
                        'handedness': handedness_label,
                        'confidence': float(confidence),
                        'gesture': gesture,
                        'fingers_up': fingers_up,
                        'finger_count': sum(fingers_up),
                        'landmarks': [
                            {
                                'id': i,
                                'x': float(lm.x),
                                'y': float(lm.y),
                                'z': float(lm.z),
                            }
                            for i, lm in enumerate(hand_lms)
                        ],
                    }
                    landmarks_data['hands'].append(hand_data)
            
            landmarks_data['hand_count'] = num_hands
            
            # Draw HUD
            self.draw_hud(frame, num_hands)
            
            # Send to C# client
            if not self.send_frame(frame, landmarks_data):
                print("[SERVER] Waiting for new client...")
                if not self.wait_for_client():
                    break
                cmd_thread = threading.Thread(target=self.receive_commands, daemon=True)
                cmd_thread.start()
            
            # FPS calculation
            self.frame_count += 1
            elapsed = time.time() - self.last_fps_time
            if elapsed >= 1.0:
                self.fps = self.frame_count / elapsed
                self.frame_count = 0
                self.last_fps_time = time.time()
        
        self.cleanup()

    def cleanup(self):
        """Release all resources."""
        self.running = False
        if self.landmarker:
            self.landmarker.close()
        if self.cap:
            self.cap.release()
        if self.client_socket:
            self.client_socket.close()
        if self.server_socket:
            self.server_socket.close()
        print("[SERVER] Cleanup done. Goodbye!")


def main():
    tracker = HandTracker(
        camera_index=0,
        max_hands=2,
        detection_confidence=0.7,
        tracking_confidence=0.6,
    )
    
    try:
        tracker.run()
    except KeyboardInterrupt:
        print("\n[SERVER] Interrupted by user")
        tracker.cleanup()


if __name__ == "__main__":
    main()
