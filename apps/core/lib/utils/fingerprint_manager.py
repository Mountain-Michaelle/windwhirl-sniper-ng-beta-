# ==============================================================
# fingerprint_manager.py - FIXED
# PATH: apps/core/lib/utils/fingerprint_manager.py
# ==============================================================

import json
import random
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class BrowserFingerprint:
    """Represents a complete browser fingerprint."""
    user_agent: str
    screen_width: int
    screen_height: int
    color_depth: int
    pixel_ratio: float
    language: str
    languages: List[str]
    platform: str
    hardware_concurrency: int
    device_memory: int
    max_touch_points: int
    timezone: str
    timezone_offset: int
    webgl_vendor: str
    webgl_renderer: str
    fingerprint_hash: str
    fonts: List[str]
    plugins: List[str]
    created_at: str


class FingerprintManager:
    """
    Manages browser fingerprints for stealth automation.
    Generates realistic, unique fingerprints and tracks used ones.
    """
    
    def __init__(self, cache_dir: str = ".fingerprints"):
        """Initialize the fingerprint manager."""
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._used_fingerprints: set = set()
        self._log = logging.getLogger(__name__)
        self._load_used_fingerprints()
    
    def _load_used_fingerprints(self) -> None:
        """Load previously used fingerprints from cache."""
        cache_file = self._cache_dir / "used_fingerprints.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                    self._used_fingerprints = set(data.get('fingerprints', []))
                self._log.debug(f"Loaded {len(self._used_fingerprints)} used fingerprints")
            except Exception as e:
                self._log.warning(f"Could not load used fingerprints: {e}")
    
    def _save_used_fingerprints(self) -> None:
        """Save used fingerprints to cache."""
        cache_file = self._cache_dir / "used_fingerprints.json"
        try:
            with open(cache_file, 'w') as f:
                json.dump({
                    'fingerprints': list(self._used_fingerprints),
                    'last_updated': datetime.now().isoformat()
                }, f)
        except Exception as e:
            self._log.warning(f"Could not save used fingerprints: {e}")
    
    def _generate_random_fingerprint(self) -> BrowserFingerprint:
        """Generate a realistic browser fingerprint."""
        
        # Random screen resolutions
        resolutions = [
            (1366, 768), (1920, 1080), (1536, 864), 
            (1440, 900), (1280, 720), (1600, 900),
            (1680, 1050), (1024, 768)
        ]
        width, height = random.choice(resolutions)
        
        # Random user agents
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        ]
        
        # Random languages
        languages_options = [
            ['en-US', 'en'],
            ['en-GB', 'en'],
            ['en-US', 'en', 'fr'],
            ['en-US', 'en', 'es'],
            ['en-CA', 'en', 'fr'],
            ['en-AU', 'en'],
        ]
        languages = random.choice(languages_options)
        
        # Random timezones
        timezones = [
            'America/New_York', 'Europe/London', 'Asia/Tokyo',
            'Australia/Sydney', 'America/Chicago', 'America/Los_Angeles',
            'Europe/Paris', 'Asia/Dubai', 'Africa/Lagos',
            'America/Toronto', 'Europe/Berlin', 'Asia/Singapore'
        ]
        
        # Random WebGL vendors
        webgl_vendors = [
            'Google Inc. (Intel)',
            'NVIDIA Corporation',
            'AMD',
            'Apple Inc.',
            'Intel Corporation'
        ]
        
        webgl_renderers = [
            'ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00009BC8) Direct3D11 vs_5_0 ps_5_0, D3D11)',
            'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)',
            'ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)',
            'Apple M1',
            'Apple M2 Pro',
            'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)'
        ]
        
        # Random fonts - FIXED: Ensure sample size doesn't exceed list length
        fonts = [
            "Arial", "Helvetica", "Times New Roman", "Courier New", 
            "Verdana", "Georgia", "Palatino", "Garamond", "Bookman",
            "Comic Sans MS", "Trebuchet MS", "Arial Black", "Impact",
            "Lucida Grande", "Tahoma", "Geneva", "Lucida Console",
            "Consolas", "Monaco", "Andale Mono", "DejaVu Sans",
            "DejaVu Serif", "DejaVu Mono", "Noto Sans", "Noto Serif",
            "Roboto", "Open Sans", "Lato", "Montserrat", "Source Sans Pro"
        ]
        # FIX: Use min() to ensure we don't request more than available
        font_count = random.randint(10, min(20, len(fonts)))
        font_subset = random.sample(fonts, font_count)
        
        # Random plugins - FIXED: Ensure sample size doesn't exceed list length
        plugins = [
            "Chrome PDF Plugin", "Chrome PDF Viewer", "Native Client",
            "Widevine Content Decryption Module", "Google Talk Plugin",
            "Google Update", "Adobe Flash Player", "Java(TM) Platform SE"
        ]
        # FIX: Use min() to ensure we don't request more than available
        plugin_count = random.randint(2, min(6, len(plugins)))
        plugin_subset = random.sample(plugins, plugin_count)
        
        # Generate fingerprint
        fingerprint = BrowserFingerprint(
            user_agent=random.choice(user_agents),
            screen_width=width,
            screen_height=height,
            color_depth=random.choice([24, 30, 32]),
            pixel_ratio=random.choice([1, 1.25, 1.5, 2]),
            language=languages[0],
            languages=languages,
            platform=random.choice(['Win32', 'MacIntel', 'Linux x86_64']),
            hardware_concurrency=random.choice([4, 6, 8, 10, 12, 16]),
            device_memory=random.choice([4, 8, 16, 32]),
            max_touch_points=random.choice([0, 1, 2, 5, 10]),
            timezone=random.choice(timezones),
            timezone_offset=random.choice([-300, -240, -180, 0, 60, 120, 180, 240, 300]),
            webgl_vendor=random.choice(webgl_vendors),
            webgl_renderer=random.choice(webgl_renderers),
            fingerprint_hash='',
            fonts=font_subset,
            plugins=plugin_subset,
            created_at=datetime.now().isoformat()
        )
        
        # Generate unique hash
        hash_string = f"{fingerprint.user_agent}{fingerprint.screen_width}{fingerprint.screen_height}{fingerprint.language}{fingerprint.timezone}{datetime.now().timestamp()}"
        fingerprint.fingerprint_hash = hashlib.md5(hash_string.encode()).hexdigest()[:16]
        
        return fingerprint
    
    def get_fresh_fingerprint(self, os_type: Optional[str] = None) -> BrowserFingerprint:
        """
        Get a fresh, unique fingerprint.
        
        Args:
            os_type: Optional OS filter ('windows', 'macos', 'linux')
        
        Returns:
            A unique BrowserFingerprint
        """
        max_attempts = 20
        
        for _ in range(max_attempts):
            fingerprint = self._generate_random_fingerprint()
            
            # Filter by OS if specified
            if os_type:
                if os_type == 'windows' and 'Windows' not in fingerprint.user_agent:
                    continue
                if os_type == 'macos' and 'Macintosh' not in fingerprint.user_agent:
                    continue
                if os_type == 'linux' and 'Linux' not in fingerprint.user_agent:
                    continue
            
            # Check if fingerprint was already used
            if fingerprint.fingerprint_hash not in self._used_fingerprints:
                self._used_fingerprints.add(fingerprint.fingerprint_hash)
                self._save_used_fingerprints()
                self._log.debug(f"Generated new fingerprint: {fingerprint.fingerprint_hash[:8]}")
                return fingerprint
        
        # Fallback: generate with timestamp to ensure uniqueness
        fingerprint = self._generate_random_fingerprint()
        fingerprint.fingerprint_hash = f"{fingerprint.fingerprint_hash}_{int(datetime.now().timestamp())}"
        return fingerprint
    
    def generate_stealth_script(self, fingerprint: BrowserFingerprint) -> str:
        """
        Generate JavaScript to inject the fingerprint into the browser.
        """
        return f"""
        // ============================================================
        // FINGERPRINT INJECTION SCRIPT
        // Generated: {datetime.now().isoformat()}
        // Fingerprint: {fingerprint.fingerprint_hash[:8]}
        // ============================================================
        
        // --- NAVIGATOR OVERRIDES ---
        Object.defineProperty(navigator, 'userAgent', {{
            get: () => '{fingerprint.user_agent}',
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(navigator, 'platform', {{
            get: () => '{fingerprint.platform}',
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(navigator, 'language', {{
            get: () => '{fingerprint.language}',
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(navigator, 'languages', {{
            get: () => {json.dumps(fingerprint.languages)},
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => {fingerprint.hardware_concurrency},
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(navigator, 'deviceMemory', {{
            get: () => {fingerprint.device_memory},
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(navigator, 'maxTouchPoints', {{
            get: () => {fingerprint.max_touch_points},
            configurable: false,
            enumerable: true
        }});
        
        // --- HIDE WEBDRIVER ---
        Object.defineProperty(navigator, 'webdriver', {{
            get: () => undefined,
            configurable: false,
            enumerable: true
        }});
        
        // --- SCREEN OVERRIDES ---
        Object.defineProperty(screen, 'width', {{
            get: () => {fingerprint.screen_width},
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(screen, 'height', {{
            get: () => {fingerprint.screen_height},
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(screen, 'colorDepth', {{
            get: () => {fingerprint.color_depth},
            configurable: false,
            enumerable: true
        }});
        
        Object.defineProperty(screen, 'pixelDepth', {{
            get: () => {fingerprint.color_depth},
            configurable: false,
            enumerable: true
        }});
        
        // --- WEBGL OVERRIDES ---
        const originalGetParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {{
            if (parameter === 0x1F00) {{ // GL_VENDOR
                return '{fingerprint.webgl_vendor}';
            }}
            if (parameter === 0x1F01) {{ // GL_RENDERER
                return '{fingerprint.webgl_renderer}';
            }}
            if (parameter === 0x1F02) {{ // GL_VERSION
                return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
            }}
            if (parameter === 0x1F03) {{ // GL_EXTENSIONS
                return 'WEBGL_debug_renderer_info WEBGL_lose_context EXT_blend_minmax EXT_texture_filter_anisotropic OES_texture_float OES_texture_half_float WEBGL_color_buffer_float WEBGL_compressed_texture_s3tc WEBGL_debug_shaders WEBGL_depth_texture WEBGL_draw_buffers WEBGL_multi_draw';
            }}
            return originalGetParameter.call(this, parameter);
        }};
        
        // --- PLUGINS OVERRIDE ---
        Object.defineProperty(navigator, 'plugins', {{
            get: () => {{
                const plugins = {json.dumps(fingerprint.plugins)};
                const pluginArray = {{
                    length: plugins.length,
                    item: function(index) {{
                        return index < this.length ? {{ 
                            name: plugins[index], 
                            filename: plugins[index] + '.plugin',
                            description: plugins[index] + ' Plugin',
                            version: '1.0.0.0'
                        }} : null;
                    }},
                    namedItem: function(name) {{
                        for(let i = 0; i < this.length; i++) {{
                            if(this.item(i).name === name) return this.item(i);
                        }}
                        return null;
                    }},
                    refresh: function() {{}},
                }};
                Object.setPrototypeOf(pluginArray, PluginArray.prototype);
                return pluginArray;
            }},
            configurable: false,
            enumerable: true
        }});
        
        // --- CHROME OBJECT OVERRIDE ---
        if (!window.chrome) {{
            Object.defineProperty(window, 'chrome', {{
                get: () => ({{
                    runtime: {{}},
                    loadTimes: function() {{}},
                    csi: function() {{}},
                    app: {{
                        isInstalled: false,
                        InstallState: {{
                            DISABLED: 'disabled',
                            INSTALLED: 'installed',
                            NOT_INSTALLED: 'not_installed'
                        }},
                        RunningState: {{
                            CANNOT_RUN: 'cannot_run',
                            READY_TO_RUN: 'ready_to_run',
                            RUNNING: 'running'
                        }}
                    }}
                }}),
                configurable: false,
                enumerable: true
            }});
        }}
        
        // --- CONSOLE WARN SUPPRESSION ---
        const originalWarn = console.warn;
        console.warn = function(msg) {{
            if (msg && typeof msg === 'string') {{
                if (msg.includes('webdriver') || msg.includes('automation') || msg.includes('headless')) {{
                    return;
                }}
            }}
            originalWarn.apply(console, arguments);
        }};
        
        // --- ADD FAKE DATA ---
        document.cookie = "fp_test=1; path=/";
        localStorage.setItem('fp_test', 'true');
        sessionStorage.setItem('fp_test', 'true');
        
        // ============================================================
        // END FINGERPRINT INJECTION
        // ============================================================
        """
    
    def clear_used_fingerprints(self) -> None:
        """Clear the cache of used fingerprints."""
        self._used_fingerprints = set()
        self._save_used_fingerprints()
        self._log.info("Cleared used fingerprints cache")