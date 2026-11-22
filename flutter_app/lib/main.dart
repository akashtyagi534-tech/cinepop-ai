import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:path_provider/path_provider.dart';
import 'package:video_player/video_player.dart';
import 'package:chewie/chewie.dart';
import 'package:google_sign_in/google_sign_in.dart';
import 'package:camera/camera.dart';
import 'package:google_mlkit_face_detection/google_mlkit_face_detection.dart';
import 'package:image/image.dart' as img;
import 'package:gal/gal.dart';

/// Ensures camera/gallery face photos are ALWAYS upright portrait (never 90° rotated or sideways).
Future<File> _fixUprightPortrait(
  File rawFile, {
  int? sensorOrientation,
  bool isFrontCamera = false,
}) async {
  try {
    final bytes = await rawFile.readAsBytes();
    final decoded = img.decodeImage(bytes);
    if (decoded == null) return rawFile;
    img.Image upright = img.bakeOrientation(decoded);
    // On some Android OEM front cameras, raw JPEG pixels are saved landscape (width > height)
    // without an EXIF orientation tag. Enforce upright portrait rotation using sensorOrientation.
    if (sensorOrientation != null && upright.width > upright.height) {
      final int rot = (sensorOrientation == 90 || sensorOrientation == 270)
          ? sensorOrientation
          : 270;
      upright = img.copyRotate(upright, angle: rot);
    }
    if (isFrontCamera) {
      upright = img.flipHorizontal(upright);
    }
    final tempDir = await getTemporaryDirectory();
    final outPath =
        "${tempDir.path}/upright_${DateTime.now().microsecondsSinceEpoch}.jpg";
    final outFile = File(outPath);
    await outFile.writeAsBytes(img.encodeJpg(upright, quality: 90), flush: true);
    return outFile;
  } catch (_) {
    return rawFile;
  }
}

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const CinePopApp());
}

class CinePopApp extends StatelessWidget {
  const CinePopApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'CinePop AI',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF0E091D),
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFFFF007F),
          secondary: Color(0xFF00E5FF),
          tertiary: Color(0xFFFFD600),
        ),
      ),
      home: const CinePopHome(),
    );
  }
}

class CinePopHome extends StatefulWidget {
  const CinePopHome({super.key});
  @override
  State<CinePopHome> createState() => _CinePopHomeState();
}

class _CinePopHomeState extends State<CinePopHome> {
  static const String _defaultRailwayUrl =
      "https://cinepop-ai-production.up.railway.app";
  String serverUrl = _defaultRailwayUrl;
  bool isServerOnline = false;
  final GoogleSignIn _googleSignIn = GoogleSignIn(scopes: ['email', 'profile']);

  String? userEmail;
  String? userName;
  String? userPhoto;
  int userCredits = 0;
  int walletBalance = 0;
  String myReferralCode = "";
  String referredBy = "";
  int referralsCount = 0;
  int referralEarnings = 0;
  List<dynamic> referralHistory = [];
  bool isAdmin = false;
  bool isAdminEmail = false;
  // Kept strictly in RAM (never stored in SharedPreferences)
  String _verifiedAdminPin = "";
  String adminUpiId = "8269045143@ybl";
  int moviePrice = 50;

  // Navigation: 0=Studio, 1=Face-Swap, 2=Library, 3=Refer & Earn
  int currentTab = 0;

  // Story Writer + Manual Script + Custom Scene Elements + Language + Watermark
  String storyMode = "ai"; // "ai" | "manual"
  String selectedGenre = "Action";
  String selectedLanguage = "Hindi";
  String defaultMood = "Action";
  String defaultLocation = "";
  String watermarkPosition = "top-right";
  File? customBgFile;
  bool isGeneratingStory = false;

  final TextEditingController aiTopicController = TextEditingController();
  final TextEditingController ideaController = TextEditingController();
  final TextEditingController locationController = TextEditingController();
  final TextEditingController watermarkController = TextEditingController();
  final TextEditingController serverController = TextEditingController();
  final TextEditingController manualEmailController = TextEditingController();
  final TextEditingController manualNameController = TextEditingController();
  final TextEditingController loginReferralController = TextEditingController();
  final TextEditingController applyReferralController = TextEditingController();
  int selectedMinutes = 1;
  String selectedAspectRatio = "9:16"; // "9:16" Instagram/Moj | "16:9" YouTube
  String selectedSeriesMode = "series"; // "series" (Moj/Pocket FM default) | "single"
  int selectedPartDuration = 1; // 1 or 2 Min / Part
  int selectedTotalParts = 3; // 3, 5, 10, 15 Parts
  List<Map<String, dynamic>> characters = [];
  String dataEstimateText =
      "📱 3 Parts × 1 Min (9:16 Moj/Reels) · Part 1 ~35 सेकंड में Play के लिए तैयार";
  bool isSubmitting = false;
  bool isInstantLoggingIn = false;

  // Active Movie Project & Studio Actions
  String? activeProjectId;
  String activeAspectRatio = "9:16";
  String activeSeriesMode = "series";
  int activeTotalParts = 3;
  int progress = 0;
  String statusMessage = "";
  List<dynamic> readyParts = [];
  Map<String, dynamic> scriptsData = {};
  bool isCompleted = false;
  bool generatingTrailer = false;
  bool generatingPoster = false;
  bool dubbingMovie = false;
  bool _autoNextTriggered = false;
  Timer? syncTimer;
  Timer? balanceTimer;

  VideoPlayerController? _videoController;
  ChewieController? _chewieController;
  String? currentlyPlayingType;

  // Custom Video AI Face-Swap Studio State
  File? fsVideoFile;
  File? fsFaceFile;
  final TextEditingController fsTitleController =
      TextEditingController(text: "My Bollywood Face-Swap");
  final TextEditingController fsDubController = TextEditingController();
  String fsDubLanguage = "Hindi";
  String fsDubPitch = "normal";
  bool fsSubmitting = false;
  String? fsActiveJobId;
  String fsStatus = "";
  String fsStatusText = "";
  Timer? fsPollTimer;
  VideoPlayerController? _fsVideoController;
  ChewieController? _fsChewieController;

  // My Movies Cloud Library State
  List<dynamic> libraryMovies = [];
  List<dynamic> libraryFaceSwaps = [];
  bool loadingLibrary = false;

  static const List<String> _genres = [
    "Action",
    "Horror",
    "Comedy",
    "Desi Superhero",
    "Love Story",
    "Thriller",
  ];

  static const List<String> _languages = [
    "Hindi",
    "Bhojpuri",
    "Haryanvi",
    "Punjabi",
    "English",
  ];

  static const List<String> _moods = [
    "Action",
    "Emotional",
    "Suspense",
    "Comedy",
    "Romantic",
  ];

  static const List<String> _sfxOptions = [
    "none",
    "rain",
    "thunder",
    "punch",
    "explosion",
    "sword",
    "footsteps",
  ];

  static const List<Map<String, String>> _pitchPresets = [
    {"key": "normal", "label": "Normal Voice"},
    {"key": "hero_deep", "label": "Heroic Deep"},
    {"key": "heavy_villain", "label": "Heavy Villain"},
    {"key": "cute_kid", "label": "Cute Kid"},
    {"key": "fast_comic", "label": "Fast Comedy"},
  ];

  static const List<Map<String, String>> _locationPresets = [
    {"key": "village_field", "label": "Gaon ka Khet"},
    {"key": "haunted_mansion", "label": "Purani Haveli"},
    {"key": "mumbai_rain", "label": "Mumbai ki Barish"},
    {"key": "deep_jungle", "label": "Deep Jungle"},
    {"key": "police_station", "label": "Police Station"},
    {"key": "outer_space", "label": "Outer Space"},
    {"key": "royal_palace", "label": "Royal Palace"},
    {"key": "city_street", "label": "Mumbai City Street"},
    {"key": "mountains", "label": "Himalayan Mountains"},
    {"key": "desert_fort", "label": "Desert Fort"},
    {"key": "courtroom", "label": "Courtroom"},
    {"key": "cyber_lab", "label": "Hi-Tech Lab"},
  ];

  @override
  void initState() {
    super.initState();
    _initApp();
  }

  Future<void> _initApp() async {
    final prefs = await SharedPreferences.getInstance();
    final savedUrl = prefs.getString('server_url');
    if (savedUrl != null &&
        savedUrl.isNotEmpty &&
        !savedUrl.contains('YOUR-RAILWAY-APP')) {
      serverUrl = savedUrl;
    } else {
      serverUrl = _defaultRailwayUrl;
      await prefs.setString('server_url', serverUrl);
    }
    serverController.text = serverUrl;
    await _checkServerHealth();

    final savedEmail = prefs.getString('user_email');
    final savedName = prefs.getString('user_name');
    final savedPhoto = prefs.getString('user_photo');
    if (savedEmail != null && savedEmail.isNotEmpty) {
      setState(() {
        userEmail = savedEmail;
        userName = savedName ?? "Creator";
        userPhoto = savedPhoto;
      });
      await _syncUserWithBackend(savedEmail, userName!, savedPhoto ?? "");
      _startBalanceAutoRefresh();
    }

    final savedId = prefs.getString('active_project_id');
    if (savedId != null && savedId.isNotEmpty) {
      setState(() => activeProjectId = savedId);
      _startAutoSync();
    }
  }

  Future<void> _checkServerHealth() async {
    try {
      final res = await http
          .get(Uri.parse('$serverUrl/'))
          .timeout(const Duration(seconds: 6));
      if (mounted) {
        setState(() => isServerOnline = (res.statusCode == 200));
      }
    } catch (_) {
      if (mounted) {
        setState(() => isServerOnline = false);
      }
    }
  }

  void _startBalanceAutoRefresh() {
    balanceTimer?.cancel();
    balanceTimer = Timer.periodic(const Duration(seconds: 8), (_) {
      _refreshUserBalance();
    });
  }

  Future<void> _refreshUserBalance() async {
    if (userEmail == null) return;
    try {
      final res = await http
          .get(
            Uri.parse(
              '$serverUrl/auth/me/?email=${Uri.encodeComponent(userEmail!)}',
            ),
          )
          .timeout(const Duration(seconds: 6));
      if (res.statusCode == 200 && mounted) {
        final data = jsonDecode(res.body);
        final u = data['user'] ?? {};
        setState(() {
          isServerOnline = true;
          userCredits = (u['credits'] ?? userCredits) as int;
          walletBalance = (u['wallet_balance'] ?? walletBalance) as int;
          myReferralCode = (u['referral_code'] ?? myReferralCode).toString();
          referredBy = (u['referred_by'] ?? referredBy).toString();
          referralsCount = (u['referrals_count'] ?? referralsCount) as int;
          referralEarnings = (u['referral_earnings'] ?? referralEarnings) as int;
          adminUpiId = data['upi_id'] ?? adminUpiId;
          moviePrice = (data['movie_price'] ?? moviePrice) as int;
        });
      }
    } catch (_) {}
  }

  @override
  void dispose() {
    syncTimer?.cancel();
    balanceTimer?.cancel();
    fsPollTimer?.cancel();
    _videoController?.dispose();
    _chewieController?.dispose();
    _fsVideoController?.dispose();
    _fsChewieController?.dispose();
    aiTopicController.dispose();
    ideaController.dispose();
    locationController.dispose();
    watermarkController.dispose();
    serverController.dispose();
    manualEmailController.dispose();
    manualNameController.dispose();
    loginReferralController.dispose();
    applyReferralController.dispose();
    fsTitleController.dispose();
    fsDubController.dispose();
    super.dispose();
  }

  Future<void> _handleGoogleSignIn() async {
    try {
      final account = await _googleSignIn
          .signIn()
          .timeout(const Duration(seconds: 6));
      if (account != null) {
        await _completeLogin(
          account.email,
          account.displayName ?? "CinePop User",
          account.photoUrl ?? "",
        );
        return;
      }
    } catch (_) {
      // Native Google Sign-In failed (e.g. ApiException: 10 without SHA-1) -> open clean Gmail Account Picker sheet
    }
    if (mounted) {
      await _showQuickGoogleIdDialog();
    }
  }

  Future<void> _handleInstantGuestLogin() async {
    if (isInstantLoggingIn) return;
    setState(() => isInstantLoggingIn = true);
    try {
      final res = await http.post(
        Uri.parse('$serverUrl/auth/instant-login/'),
        body: {
          "device_id": Platform.operatingSystem,
          "referral_code": loginReferralController.text.trim(),
        },
      ).timeout(const Duration(seconds: 8));
      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        final u = data['user'] ?? {};
        final em = (u['email'] ?? "guest_${DateTime.now().millisecondsSinceEpoch}@cinepop.guest")
            .toString();
        final nm = (u['name'] ?? "Guest Creator").toString();
        final prefs = await SharedPreferences.getInstance();
        await prefs.setString('user_email', em);
        await prefs.setString('user_name', nm);
        await prefs.setString('user_photo', "");
        if (mounted) {
          setState(() {
            isServerOnline = true;
            userEmail = em;
            userName = nm;
            userPhoto = "";
            userCredits = (u['credits'] ?? 1) as int;
            walletBalance = (u['wallet_balance'] ?? 0) as int;
            myReferralCode = (u['referral_code'] ?? "").toString();
            isAdmin = false;
            isAdminEmail = false;
            _verifiedAdminPin = "";
            adminUpiId = (data['upi_id'] ?? adminUpiId).toString();
            moviePrice = (data['movie_price'] ?? moviePrice) as int;
          });
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              backgroundColor: Colors.green.shade700,
              content: Text(
                (data['message'] ??
                        "⚡ Instant Start तैयार है! आपको 1 Free Trial Pass मिल गया है।")
                    .toString(),
              ),
            ),
          );
          _startBalanceAutoRefresh();
        }
        return;
      }
    } catch (_) {
      // Offline / transient fallback so guest still enters smoothly
    } finally {
      if (mounted) {
        setState(() => isInstantLoggingIn = false);
      }
    }
    // Fallback local guest entry synced immediately on next call
    final fallbackEmail =
        "guest_${DateTime.now().millisecondsSinceEpoch}@cinepop.guest";
    await _completeLogin(fallbackEmail, "Guest Creator", "");
  }

  Future<void> _showQuickGoogleIdDialog() async {
    await showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFF1B1236),
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          left: 20,
          right: 20,
          top: 20,
          bottom: MediaQuery.of(ctx).viewInsets.bottom + 24,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Center(
              child: Container(
                width: 44,
                height: 4,
                decoration: BoxDecoration(
                  color: Colors.white24,
                  borderRadius: BorderRadius.circular(10),
                ),
              ),
            ),
            const SizedBox(height: 16),
            const Row(
              children: [
                Icon(Icons.account_circle, color: Color(0xFFFFD600), size: 28),
                SizedBox(width: 10),
                Expanded(
                  child: Text(
                    "Google Account से तुरंत लॉगिन करें",
                    style: TextStyle(
                      color: Color(0xFFFFD600),
                      fontSize: 17,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 6),
            const Text(
              "अपनी Gmail ID और नाम डालें — बिना किसी SHA-1 या Google Play एरर के 1 क्लिक में लॉगिन होगा।",
              style: TextStyle(color: Colors.white70, fontSize: 12),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: manualEmailController,
              keyboardType: TextInputType.emailAddress,
              autofocus: true,
              decoration: const InputDecoration(
                filled: true,
                fillColor: Color(0xFF0E091D),
                border: OutlineInputBorder(),
                prefixIcon: Icon(Icons.email, color: Color(0xFF00E5FF)),
                labelText: "आपकी Gmail ID",
                hintText: "example@gmail.com",
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: manualNameController,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(
                filled: true,
                fillColor: Color(0xFF0E091D),
                border: OutlineInputBorder(),
                prefixIcon: Icon(Icons.person, color: Color(0xFFFFD600)),
                labelText: "आपका नाम (Creator Name)",
                hintText: "जैसे: Rahul Sharma",
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: loginReferralController,
              textCapitalization: TextCapitalization.characters,
              decoration: const InputDecoration(
                filled: true,
                fillColor: Color(0xFF0E091D),
                border: OutlineInputBorder(),
                prefixIcon: Icon(Icons.card_giftcard, color: Colors.greenAccent),
                labelText: "🎁 दोस्त का Referral Code (Optional)",
                hintText: "जैसे: CPABC123 (+1 Free Movie Pass)",
              ),
            ),
            const SizedBox(height: 16),
            ElevatedButton.icon(
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFFFF007F),
                padding: const EdgeInsets.symmetric(vertical: 14),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                ),
              ),
              onPressed: () async {
                final em = manualEmailController.text.trim().toLowerCase();
                final nm = manualNameController.text.trim();
                if (!em.contains("@")) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text("कृपया सही Gmail ID डालें (जैसे: name@gmail.com)"),
                    ),
                  );
                  return;
                }
                Navigator.pop(ctx);
                await _completeLogin(
                  em,
                  nm.isNotEmpty ? nm : em.split("@")[0],
                  "",
                );
              },
              icon: const Icon(Icons.login, color: Colors.white),
              label: const Text(
                "1-Click Google Login करें",
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 15,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ),
            const SizedBox(height: 10),
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                side: const BorderSide(color: Color(0xFF00E5FF)),
                padding: const EdgeInsets.symmetric(vertical: 12),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                ),
              ),
              onPressed: () {
                Navigator.pop(ctx);
                _handleInstantGuestLogin();
              },
              icon: const Icon(Icons.bolt, color: Color(0xFF00E5FF)),
              label: const Text(
                "⚡ बिना ईमेल के Guest / Instant Start करें",
                style: TextStyle(
                  color: Color(0xFF00E5FF),
                  fontWeight: FontWeight.bold,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _completeLogin(String email, String name, String photo) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('user_email', email);
    await prefs.setString('user_name', name);
    await prefs.setString('user_photo', photo);

    setState(() {
      userEmail = email;
      userName = name;
      userPhoto = photo;
      isAdmin = false;
      _verifiedAdminPin = "";
    });

    await _syncUserWithBackend(
      email,
      name,
      photo,
      referralCode: loginReferralController.text.trim(),
    );
    _startBalanceAutoRefresh();
  }

  Future<void> _syncUserWithBackend(
    String email,
    String name,
    String photo, {
    String adminPin = "",
    String referralCode = "",
  }) async {
    try {
      final res = await http.post(
        Uri.parse('$serverUrl/auth/google-login/'),
        body: {
          "email": email,
          "name": name,
          "photo_url": photo,
          "device_id": Platform.operatingSystem,
          "admin_pin": adminPin,
          "referral_code": referralCode,
        },
      ).timeout(const Duration(seconds: 8));
      if (res.statusCode == 200 && mounted) {
        final data = jsonDecode(res.body);
        final u = data['user'] ?? {};
        setState(() {
          isServerOnline = true;
          userCredits = (u['credits'] ?? 0) as int;
          walletBalance = (u['wallet_balance'] ?? 0) as int;
          myReferralCode = (u['referral_code'] ?? "").toString();
          referredBy = (u['referred_by'] ?? "").toString();
          referralsCount = (u['referrals_count'] ?? 0) as int;
          referralEarnings = (u['referral_earnings'] ?? 0) as int;
          isAdmin = data['is_admin'] == true;
          isAdminEmail =
              (data['is_admin_email'] == true) || (data['is_admin'] == true);
          adminUpiId = data['upi_id'] ?? "8269045143@ybl";
          moviePrice = (data['movie_price'] ?? 50) as int;
        });
        final refMsg = (data['referral_message'] ?? "").toString();
        if (refMsg.isNotEmpty && mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text(refMsg)),
          );
        }
      }
    } catch (_) {}
  }

  Future<void> _openAdminPanelWithPinLock() async {
    if (userEmail == null) return;
    if (isAdmin && _verifiedAdminPin.length == 6) {
      await Navigator.push(
        context,
        MaterialPageRoute(
          builder: (_) => AdminDashboardScreen(
            serverUrl: serverUrl,
            adminEmail: userEmail!,
            adminPin: _verifiedAdminPin,
          ),
        ),
      );
      await _refreshUserBalance();
      return;
    }

    final pinCtrl = TextEditingController();
    String? pinError;
    bool verifying = false;

    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setModalState) => AlertDialog(
          backgroundColor: const Color(0xFF1B1236),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(18)),
          title: const Text(
            "🔐 6-Digit Secret Admin PIN",
            style: TextStyle(
              color: Color(0xFFFFD600),
              fontWeight: FontWeight.bold,
              fontSize: 18,
            ),
          ),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Text(
                "Admin Control Panel खोलने के लिए अपना 6 अंकों का Secret PIN डालें:",
                style: TextStyle(fontSize: 12, color: Colors.white70),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: pinCtrl,
                obscureText: true,
                maxLength: 6,
                keyboardType: TextInputType.number,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 22,
                  letterSpacing: 8,
                  fontWeight: FontWeight.bold,
                ),
                decoration: const InputDecoration(
                  border: OutlineInputBorder(),
                  hintText: "••••••",
                  counterText: "",
                ),
              ),
              if (pinError != null) ...[
                const SizedBox(height: 8),
                Text(
                  pinError!,
                  style: const TextStyle(color: Colors.redAccent, fontSize: 12),
                ),
              ],
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: const Text("रद्द करें"),
            ),
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFFFF007F),
              ),
              onPressed: verifying
                  ? null
                  : () async {
                      final entered = pinCtrl.text.trim();
                      if (entered.length != 6) {
                        setModalState(
                          () => pinError = "कृपया पूरा 6-Digit PIN डालें।",
                        );
                        return;
                      }
                      setModalState(() {
                        verifying = true;
                        pinError = null;
                      });
                      try {
                        final res = await http.post(
                          Uri.parse('$serverUrl/admin/verify-pin/'),
                          body: {
                            "email": userEmail!,
                            "admin_pin": entered,
                          },
                        ).timeout(const Duration(seconds: 8));
                        if (res.statusCode == 200) {
                          setState(() {
                            isAdmin = true;
                            _verifiedAdminPin = entered;
                          });
                          if (ctx.mounted) Navigator.pop(ctx);
                          if (mounted) {
                            await Navigator.push(
                              context,
                              MaterialPageRoute(
                                builder: (_) => AdminDashboardScreen(
                                  serverUrl: serverUrl,
                                  adminEmail: userEmail!,
                                  adminPin: entered,
                                ),
                              ),
                            );
                            await _refreshUserBalance();
                          }
                        } else {
                          final errData = jsonDecode(res.body);
                          setModalState(() {
                            verifying = false;
                            pinError = errData['detail'] ?? "गलत Admin PIN!";
                          });
                        }
                      } catch (_) {
                        setModalState(() {
                          verifying = false;
                          pinError = "सर्वर से कनेक्ट नहीं हो पाया।";
                        });
                      }
                    },
              child: Text(
                verifying ? "Checking..." : "Unlock Panel",
                style: const TextStyle(color: Colors.white),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _logout() async {
    try {
      await _googleSignIn.signOut();
    } catch (_) {}
    balanceTimer?.cancel();
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('user_email');
    setState(() {
      userEmail = null;
      isAdmin = false;
      isAdminEmail = false;
      _verifiedAdminPin = "";
      userCredits = 0;
      walletBalance = 0;
    });
  }

  void _updateDataEstimate() {
    final String ratioLabel = selectedAspectRatio == "9:16"
        ? "9:16 Instagram/Moj"
        : "16:9 YouTube HD";
    if (selectedSeriesMode == "series") {
      final int totalMins = selectedPartDuration * selectedTotalParts;
      final double totalMb = characters.length * 1.5 + totalMins * 14.0;
      setState(() {
        dataEstimateText =
            "📱 $selectedTotalParts Parts × $selectedPartDuration Min ($ratioLabel · ~${totalMb.toStringAsFixed(0)} MB) · Part 1 सबसे पहले ~35s में Play होगा";
      });
      return;
    }
    double uploadMb = characters.length * 1.5;
    double downloadMb = selectedMinutes * 16.0;
    double totalMb = uploadMb + downloadMb;
    String speedHint = selectedMinutes <= 1
        ? "12-14 तेज़ सीन्स · ~35-45 सेकंड में तैयार"
        : selectedMinutes <= 2
            ? "18-22 तेज़ सीन्स · सुपर-फास्ट रेंडर"
            : selectedMinutes <= 5
                ? "तेज़ कट्स · शॉर्ट फिल्म"
                : selectedMinutes <= 10
                    ? "फुल एपिसोड"
                    : "फुल मूवी";
    setState(() {
      dataEstimateText =
          "🎬 $selectedMinutes Min ($ratioLabel · ~${totalMb.toStringAsFixed(0)} MB · $speedHint)";
    });
  }

  Future<void> _configureServerUrl() async {
    await showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: const Text(
          "⚙️ सर्वर URL सेट करें",
          style: TextStyle(color: Color(0xFF00E5FF)),
        ),
        content: TextField(
          controller: serverController,
          decoration: const InputDecoration(
            hintText: "https://your-cinepop.up.railway.app",
            labelText: "Railway / Backend URL",
          ),
        ),
        actions: [
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFFFF007F),
            ),
            onPressed: () async {
              final cleaned =
                  serverController.text.trim().replaceAll(RegExp(r'/+$'), '');
              final prefs = await SharedPreferences.getInstance();
              await prefs.setString('server_url', cleaned);
              setState(() => serverUrl = cleaned);
              await _checkServerHealth();
              if (userEmail != null) {
                await _syncUserWithBackend(
                  userEmail!,
                  userName!,
                  userPhoto ?? "",
                  adminPin: _verifiedAdminPin,
                );
              }
              if (mounted) Navigator.pop(ctx);
            },
            child: const Text(
              "सेव करें",
              style: TextStyle(color: Colors.white),
            ),
          )
        ],
      ),
    );
  }

  Future<void> _showBuyCreditDialog() async {
    await _refreshUserBalance();
    TextEditingController utrCtrl = TextEditingController();
    TextEditingController promoCtrl = TextEditingController();
    int qty = 1;

    if (!mounted) return;
    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setModalState) => AlertDialog(
          backgroundColor: const Color(0xFF1B1236),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
          title: const Text(
            "💳 वॉलेट, मूवी पास & Promo",
            style: TextStyle(
              color: Color(0xFFFFD600),
              fontWeight: FontWeight.bold,
              fontSize: 18,
            ),
          ),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                // Live Wallet & Movie Pass Summary
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: const Color(0xFF102A26),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: Colors.greenAccent),
                  ),
                  child: Column(
                    children: [
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Text(
                            "वॉलेट: ₹$walletBalance",
                            style: const TextStyle(
                              fontWeight: FontWeight.bold,
                              color: Colors.greenAccent,
                              fontSize: 15,
                            ),
                          ),
                          Text(
                            "पास: $userCredits मूवी",
                            style: const TextStyle(
                              fontWeight: FontWeight.bold,
                              color: Color(0xFFFFD600),
                              fontSize: 15,
                            ),
                          ),
                        ],
                      ),
                      if (walletBalance >= moviePrice) ...[
                        const SizedBox(height: 8),
                        SizedBox(
                          width: double.infinity,
                          child: ElevatedButton(
                            style: ElevatedButton.styleFrom(
                              backgroundColor: Colors.green,
                            ),
                            onPressed: () async {
                              final res = await http.post(
                                Uri.parse(
                                  '$serverUrl/billing/buy-pass-from-wallet/',
                                ),
                                body: {
                                  "email": userEmail!,
                                  "movies_count": "1",
                                },
                              );
                              final data = jsonDecode(res.body);
                              if (res.statusCode == 200) {
                                final u = data['user'] ?? {};
                                setState(() {
                                  walletBalance =
                                      (u['wallet_balance'] ?? walletBalance)
                                          as int;
                                  userCredits =
                                      (u['credits'] ?? userCredits) as int;
                                });
                                setModalState(() {});
                                if (mounted) {
                                  ScaffoldMessenger.of(context).showSnackBar(
                                    SnackBar(
                                      content: Text(data['message'] ?? "Done"),
                                    ),
                                  );
                                }
                              }
                            },
                            child: Text(
                              "₹$moviePrice वॉलेट से 1 मूवी पास खरीदें",
                              style: const TextStyle(color: Colors.white),
                            ),
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: const Color(0xFF28144A),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: const Color(0xFFFFD600)),
                  ),
                  child: Column(
                    children: [
                      const Text(
                        "फ्री Promo Code है तो यहाँ डालें:",
                        style:
                            TextStyle(fontSize: 12, color: Color(0xFFFFD600)),
                      ),
                      const SizedBox(height: 6),
                      Row(
                        children: [
                          Expanded(
                            child: TextField(
                              controller: promoCtrl,
                              textCapitalization: TextCapitalization.characters,
                              decoration: const InputDecoration(
                                isDense: true,
                                border: OutlineInputBorder(),
                                hintText: "जैसे: DOSTFREE",
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          ElevatedButton(
                            style: ElevatedButton.styleFrom(
                              backgroundColor: Colors.green,
                            ),
                            onPressed: () async {
                              if (promoCtrl.text.trim().isEmpty) return;
                              final res = await http.post(
                                Uri.parse('$serverUrl/billing/redeem-promo/'),
                                body: {
                                  "email": userEmail!,
                                  "code": promoCtrl.text.trim(),
                                },
                              );
                              final data = jsonDecode(res.body);
                              if (res.statusCode == 200) {
                                setState(
                                  () => userCredits =
                                      (data['new_credits'] ?? userCredits)
                                          as int,
                                );
                                if (mounted) {
                                  Navigator.pop(ctx);
                                  ScaffoldMessenger.of(context).showSnackBar(
                                    SnackBar(content: Text(data['message'])),
                                  );
                                }
                              } else {
                                if (mounted) {
                                  ScaffoldMessenger.of(context).showSnackBar(
                                    SnackBar(
                                      content: Text(
                                        data['detail'] ?? "Invalid Promo Code",
                                      ),
                                    ),
                                  );
                                }
                              }
                            },
                            child: const Text(
                              "Apply",
                              style: TextStyle(color: Colors.white),
                            ),
                          )
                        ],
                      )
                    ],
                  ),
                ),
                const Divider(height: 22),
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: Colors.black45,
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: const Color(0xFF00E5FF)),
                  ),
                  child: Column(
                    children: [
                      Text(
                        "या UPI से ₹$moviePrice प्रति मूवी पेमेंट करें:",
                        style: const TextStyle(fontSize: 12),
                      ),
                      const SizedBox(height: 4),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Text(
                            adminUpiId,
                            style: const TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.bold,
                              color: Color(0xFF00E5FF),
                            ),
                          ),
                          IconButton(
                            icon: const Icon(
                              Icons.copy,
                              size: 18,
                              color: Colors.white,
                            ),
                            onPressed: () {
                              Clipboard.setData(
                                ClipboardData(text: adminUpiId),
                              );
                              ScaffoldMessenger.of(context).showSnackBar(
                                const SnackBar(
                                  content: Text("UPI ID कॉपी हो गई!"),
                                ),
                              );
                            },
                          )
                        ],
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 10),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    const Text("कितनी मूवी बनानी है?"),
                    DropdownButton<int>(
                      value: qty,
                      items: [1, 2, 5, 10]
                          .map(
                            (e) => DropdownMenuItem(
                              value: e,
                              child: Text("$e मूवी (₹${e * moviePrice})"),
                            ),
                          )
                          .toList(),
                      onChanged: (v) => setModalState(() => qty = v!),
                    )
                  ],
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: utrCtrl,
                  decoration: const InputDecoration(
                    border: OutlineInputBorder(),
                    labelText: "पेमेंट UTR / Transaction No.",
                    hintText: "12 अंकों का UPI ट्रांजैक्शन नंबर",
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () async {
                await _refreshUserBalance();
                setModalState(() {});
              },
              child: const Text("Sync Balance"),
            ),
            TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: const Text("बंद करें"),
            ),
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFFFF007F),
              ),
              onPressed: () async {
                if (utrCtrl.text.trim().isEmpty) return;
                final res = await http.post(
                  Uri.parse('$serverUrl/billing/submit-payment/'),
                  body: {
                    "email": userEmail!,
                    "utr_number": utrCtrl.text.trim(),
                    "movies_count": qty.toString(),
                  },
                );
                if (mounted) {
                  Navigator.pop(ctx);
                  final data = jsonDecode(res.body);
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(content: Text(data['message'])),
                  );
                }
              },
              child: const Text(
                "पेमेंट सबमिट करें",
                style: TextStyle(color: Colors.white),
              ),
            )
          ],
        ),
      ),
    );
  }

  Future<void> _generateStoryWithAI() async {
    final topic = aiTopicController.text.trim().isNotEmpty
        ? aiTopicController.text.trim()
        : ideaController.text.trim();
    if (topic.length < 4) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text("कृपया 1 लाइन का टॉपिक लिखें ताकि AI पूरी कहानी बना सके!"),
        ),
      );
      return;
    }

    setState(() => isGeneratingStory = true);
    try {
      final charNamesStr = characters.isNotEmpty
          ? characters.map((c) => c['name']).join(',')
          : "Vikram, Kaal";
      final res = await http.post(
        Uri.parse('$serverUrl/ai/generate-story/'),
        body: {
          "topic": topic,
          "genre": selectedGenre,
          "char_names": charNamesStr,
          "duration_minutes": selectedMinutes.toString(),
        },
      ).timeout(const Duration(seconds: 30));

      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        setState(() {
          ideaController.text = data['story'] ?? "";
        });
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text("✅ AI ने पूरी कहानी और डायलॉग्स लिख दिए हैं! आप एडिट भी कर सकते हैं।"),
            ),
          );
        }
      } else {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text("AI Story बनाने में समस्या आई।")),
          );
        }
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text("सर्वर से कनेक्शन फेल। ऊपर ⚙️ में Railway URL चेक करें।"),
          ),
        );
      }
    } finally {
      if (mounted) setState(() => isGeneratingStory = false);
    }
  }

  Future<void> _pickCustomBackground() async {
    final picker = ImagePicker();
    final XFile? photo =
        await picker.pickImage(source: ImageSource.gallery, imageQuality: 85);
    if (photo != null) {
      setState(() => customBgFile = File(photo.path));
    }
  }

  static const List<Map<String, String>> _faceAngleSteps = [
    {
      "key": "front",
      "label": "Front",
      "hi": "सामने देखें (Look Straight)",
      "hint": "चेहरा सर्कल के ठीक बीच में सीधा रखें",
    },
    {
      "key": "left",
      "label": "Left",
      "hi": "थोड़ा बाएं मुड़ें (Turn Left)",
      "hint": "चेहरे को हल्का सा बाईं ओर घुमाएँ",
    },
    {
      "key": "right",
      "label": "Right",
      "hi": "थोड़ा दाएं मुड़ें (Turn Right)",
      "hint": "चेहरे को हल्का सा दाईं ओर घुमाएँ",
    },
    {
      "key": "up",
      "label": "Up",
      "hi": "थोड़ा ऊपर देखें (Look Up)",
      "hint": "ठोड़ी को थोड़ा ऊपर उठाएँ",
    },
    {
      "key": "down",
      "label": "Down",
      "hi": "थोड़ा नीचे देखें (Look Down)",
      "hint": "चेहरे को हल्का सा नीचे झुकाएँ",
    },
  ];

  IconData _angleIcon(String key) {
    switch (key) {
      case "left":
        return Icons.arrow_back_rounded;
      case "right":
        return Icons.arrow_forward_rounded;
      case "up":
        return Icons.arrow_upward_rounded;
      case "down":
        return Icons.arrow_downward_rounded;
      default:
        return Icons.face_retouching_natural;
    }
  }

  Future<void> _addCharacter() async {
    if (!mounted) return;
    final String? chosenMode = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: const Color(0xFF160E2B),
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(22)),
      ),
      builder: (sheetCtx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 18, 20, 22),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Row(
                children: [
                  Icon(Icons.face_retouching_natural, color: Color(0xFFFF007F)),
                  SizedBox(width: 8),
                  Text(
                    "🎭 3D Face Scan — कैरेक्टर जोड़ें",
                    style: TextStyle(
                      color: Color(0xFFFFD600),
                      fontWeight: FontWeight.bold,
                      fontSize: 16,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              const Text(
                "हर एंगल (Front, Left, Right, Up, Down) से असली चेहरा लॉक करें ताकि साइड और एक्शन शॉट में भी चेहरा न बिगड़े:",
                style: TextStyle(color: Colors.white70, fontSize: 12),
              ),
              const SizedBox(height: 16),
              InkWell(
                borderRadius: BorderRadius.circular(14),
                onTap: () => Navigator.pop(sheetCtx, "live"),
                child: Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    gradient: const LinearGradient(
                      colors: [Color(0xFFFF007F), Color(0xFF7000FF)],
                    ),
                    borderRadius: BorderRadius.circular(14),
                  ),
                  child: const Row(
                    children: [
                      Icon(Icons.camera_front, color: Colors.white, size: 28),
                      SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              "📸 Live 5-Angle Face Scan (Recommended)",
                              style: TextStyle(
                                color: Colors.white,
                                fontWeight: FontWeight.bold,
                                fontSize: 13,
                              ),
                            ),
                            SizedBox(height: 2),
                            Text(
                              "Face ID / KYC जैसा गाइडेड स्कैन — Front, Left, Right, Up, Down",
                              style: TextStyle(
                                color: Colors.white70,
                                fontSize: 11,
                              ),
                            ),
                          ],
                        ),
                      ),
                      Icon(Icons.chevron_right, color: Colors.white),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 10),
              InkWell(
                borderRadius: BorderRadius.circular(14),
                onTap: () => Navigator.pop(sheetCtx, "gallery"),
                child: Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: const Color(0xFF231747),
                    borderRadius: BorderRadius.circular(14),
                    border: Border.all(color: const Color(0xFF00E5FF)),
                  ),
                  child: const Row(
                    children: [
                      Icon(
                        Icons.photo_library,
                        color: Color(0xFF00E5FF),
                        size: 26,
                      ),
                      SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              "🖼️ Gallery से Multiple Angles Upload करें",
                              style: TextStyle(
                                color: Colors.white,
                                fontWeight: FontWeight.bold,
                                fontSize: 13,
                              ),
                            ),
                            SizedBox(height: 2),
                            Text(
                              "Front, Left, Right, Up, Down की अलग-अलग फोटो चुनें",
                              style: TextStyle(
                                color: Colors.white70,
                                fontSize: 11,
                              ),
                            ),
                          ],
                        ),
                      ),
                      Icon(Icons.chevron_right, color: Color(0xFF00E5FF)),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );

    if (chosenMode == null || !mounted) return;
    if (chosenMode == "live") {
      await _openLive5AngleFaceScan();
    } else {
      await _openGalleryMultiAngleDialog();
    }
  }

  Future<void> _openLive5AngleFaceScan() async {
    if (!mounted) return;
    final Map<String, File>? scannedAngles =
        await Navigator.push<Map<String, File>>(
      context,
      MaterialPageRoute(
        builder: (_) => const HandsFreeFaceIdScannerPage(),
      ),
    );
    if (scannedAngles != null && scannedAngles.isNotEmpty && mounted) {
      await _showCharacterDetailsDialog(scannedAngles);
    }
  }

  Future<void> _openGalleryMultiAngleDialog() async {
    final picker = ImagePicker();
    final Map<String, File> selectedAngles = {};

    if (!mounted) return;
    final bool? proceed = await showDialog<bool>(
      context: context,
      builder: (dlgCtx) => StatefulBuilder(
        builder: (ctx, setDlgState) {
          final int count = selectedAngles.length;
          return AlertDialog(
            backgroundColor: const Color(0xFF140D29),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(18),
              side: const BorderSide(color: Color(0xFF00E5FF)),
            ),
            title: Text(
              "🖼️ Multiple Angles चुनें ($count/5)",
              style: const TextStyle(
                color: Color(0xFFFFD600),
                fontSize: 15,
                fontWeight: FontWeight.bold,
              ),
            ),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Text(
                    "कम से कम Front फोटो चुनें। 5 एंगल (Front, Left, Right, Up, Down) चुनने से 3D Face Profile लॉक होता है।",
                    style: TextStyle(color: Colors.white70, fontSize: 11),
                  ),
                  const SizedBox(height: 12),
                  ..._faceAngleSteps.map((step) {
                    final k = step['key']!;
                    final File? file = selectedAngles[k];
                    return Container(
                      margin: const EdgeInsets.only(bottom: 8),
                      decoration: BoxDecoration(
                        color: file != null
                            ? Colors.green.withOpacity(0.14)
                            : const Color(0xFF1F1440),
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(
                          color: file != null
                              ? Colors.greenAccent
                              : Colors.white24,
                        ),
                      ),
                      child: ListTile(
                        dense: true,
                        leading: file != null
                            ? ClipRRect(
                                borderRadius: BorderRadius.circular(8),
                                child: Image.file(
                                  file,
                                  width: 42,
                                  height: 42,
                                  fit: BoxFit.cover,
                                ),
                              )
                            : CircleAvatar(
                                backgroundColor: const Color(0xFF0B0718),
                                child: Icon(
                                  _angleIcon(k),
                                  color: const Color(0xFF00E5FF),
                                  size: 20,
                                ),
                              ),
                        title: Text(
                          "${step['label']}${k == 'front' ? ' *' : ''}",
                          style: const TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.bold,
                            fontSize: 13,
                          ),
                        ),
                        subtitle: Text(
                          step['hi']!,
                          style: const TextStyle(
                            color: Colors.white60,
                            fontSize: 11,
                          ),
                        ),
                        trailing: Icon(
                          file != null ? Icons.check_circle : Icons.add_a_photo,
                          color: file != null
                              ? Colors.greenAccent
                              : const Color(0xFFFFD600),
                        ),
                        onTap: () async {
                          final XFile? pic = await picker.pickImage(
                            source: ImageSource.gallery,
                            imageQuality: 85,
                          );
                          if (pic != null) {
                            final uprightFile =
                                await _fixUprightPortrait(File(pic.path));
                            setDlgState(() {
                              selectedAngles[k] = uprightFile;
                            });
                          }
                        },
                      ),
                    );
                  }),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(dlgCtx, false),
                child: const Text(
                  "Cancel",
                  style: TextStyle(color: Colors.white60),
                ),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFFF007F),
                ),
                onPressed: selectedAngles.isEmpty
                    ? null
                    : () => Navigator.pop(dlgCtx, true),
                child: Text(
                  count >= 5
                      ? "✅ 5-Angle Ready — आगे बढ़ें"
                      : "आगे बढ़ें ($count Angles)",
                  style: const TextStyle(color: Colors.white),
                ),
              ),
            ],
          );
        },
      ),
    );

    if (proceed == true && selectedAngles.isNotEmpty && mounted) {
      await _showCharacterDetailsDialog(selectedAngles);
    }
  }

  Future<void> _showCharacterDetailsDialog(
    Map<String, File> angleFiles,
  ) async {
    final File primaryPhoto =
        angleFiles['front'] ?? angleFiles.values.first;
    final TextEditingController nameCtrl = TextEditingController();
    String selectedVoice = "hi-IN-MadhurNeural";
    String selectedPitch = "normal";
    final int angleCount = angleFiles.length;

    if (!mounted) return;
    await showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: const Text(
          "🎭 कैरेक्टर का नाम और आवाज़ चुनें",
          style: TextStyle(color: Color(0xFFFFD600), fontSize: 15),
        ),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (angleCount >= 5)
                Container(
                  width: double.infinity,
                  margin: const EdgeInsets.only(bottom: 10),
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 8,
                  ),
                  decoration: BoxDecoration(
                    color: Colors.green.withOpacity(0.18),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: Colors.greenAccent),
                  ),
                  child: const Text(
                    "✅ 5-Angle 3D Face Profile Ready!",
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color: Colors.greenAccent,
                      fontWeight: FontWeight.bold,
                      fontSize: 12,
                    ),
                  ),
                ),
              TextField(
                controller: nameCtrl,
                decoration: const InputDecoration(
                  labelText: "नाम (जैसे: Vikram, Hero, Villain)",
                ),
              ),
              const SizedBox(height: 10),
              DropdownButtonFormField<String>(
                value: selectedVoice,
                items: const [
                  DropdownMenuItem(
                    value: "hi-IN-MadhurNeural",
                    child: Text("लड़का (हिंदी / रीजनल)"),
                  ),
                  DropdownMenuItem(
                    value: "hi-IN-SwaraNeural",
                    child: Text("लड़की (हिंदी / रीजनल)"),
                  ),
                  DropdownMenuItem(
                    value: "en-IN-PrabhatNeural",
                    child: Text("English / भारी आवाज़"),
                  ),
                ],
                onChanged: (val) => selectedVoice = val!,
                decoration: const InputDecoration(labelText: "आवाज़ चुनें"),
              ),
              const SizedBox(height: 10),
              DropdownButtonFormField<String>(
                value: selectedPitch,
                items: _pitchPresets
                    .map(
                      (p) => DropdownMenuItem(
                        value: p['key'],
                        child: Text(p['label']!),
                      ),
                    )
                    .toList(),
                onChanged: (val) => selectedPitch = val!,
                decoration:
                    const InputDecoration(labelText: "Voice Pitch / Speed"),
              ),
            ],
          ),
        ),
        actions: [
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFFFF007F),
            ),
            onPressed: () {
              final cleanName = nameCtrl.text.trim().isEmpty
                  ? "Actor ${characters.length + 1}"
                  : nameCtrl.text.trim();
              setState(() {
                characters.add({
                  "name": cleanName,
                  "voice": selectedVoice,
                  "pitch": selectedPitch,
                  "file": primaryPhoto,
                  "angles": Map<String, File>.from(angleFiles),
                  "is5AngleReady": angleCount >= 5,
                });
              });
              _updateDataEstimate();
              Navigator.pop(ctx);
              if (angleCount >= 5 && mounted) {
                ScaffoldMessenger.of(context).showSnackBar(
                  SnackBar(
                    content: Text(
                      "✅ $cleanName: 5-Angle 3D Face Profile Ready!",
                    ),
                    backgroundColor: Colors.green.shade700,
                  ),
                );
              }
            },
            child: const Text(
              "सेव करें",
              style: TextStyle(color: Colors.white),
            ),
          )
        ],
      ),
    );
  }

  Future<void> _startAutonomousMovie() async {
    if (isSubmitting) return;
    final canAfford =
        isAdmin || userCredits >= 1 || walletBalance >= moviePrice;
    if (!canAfford) {
      _showBuyCreditDialog();
      return;
    }

    final finalIdea = ideaController.text.trim().isNotEmpty
        ? ideaController.text.trim()
        : aiTopicController.text.trim();

    if (finalIdea.isEmpty || characters.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text("कहानी और कम से कम 1 कैरेक्टर की फोटो ज़रूरी है!"),
        ),
      );
      return;
    }

    setState(() {
      isSubmitting = true;
      statusMessage = "फोटोज़, लोकेशन और कहानी क्लाउड पर भेजी जा रही है...";
    });

    try {
      var request = http.MultipartRequest(
        'POST',
        Uri.parse('$serverUrl/start-autonomous-movie/'),
      );
      request.fields['user_email'] = userEmail!;
      request.fields['idea'] = finalIdea;
      request.fields['story_mode'] = storyMode;
      request.fields['genre'] = selectedGenre;
      request.fields['language'] = selectedLanguage;
      request.fields['default_mood'] = defaultMood;
      request.fields['default_location'] = locationController.text.trim();
      request.fields['watermark_text'] = watermarkController.text.trim();
      request.fields['watermark_position'] = watermarkPosition;
      request.fields['aspect_ratio'] = selectedAspectRatio;
      request.fields['platform'] =
          selectedAspectRatio == "9:16" ? "instagram" : "youtube";
      request.fields['series_mode'] = selectedSeriesMode;
      request.fields['part_duration_minutes'] = selectedPartDuration.toString();
      request.fields['total_parts'] = selectedTotalParts.toString();
      request.fields['duration_minutes'] = selectedSeriesMode == "series"
          ? (selectedPartDuration * selectedTotalParts).toString()
          : selectedMinutes.toString();
      request.fields['char_names'] =
          characters.map((c) => c['name']).join(',');
      request.fields['voices'] = characters.map((c) => c['voice']).join(',');
      final Map<String, String> pitchMap = {};
      for (var c in characters) {
        pitchMap[c['name'].toString()] = (c['pitch'] ?? 'normal').toString();
      }
      request.fields['char_pitches'] = jsonEncode(pitchMap);
      if (_verifiedAdminPin.isNotEmpty) {
        request.fields['admin_pin'] = _verifiedAdminPin;
      }

      final List<Map<String, dynamic>> angleMetaList = [];
      for (int i = 0; i < characters.length; i++) {
        final c = characters[i];
        request.files.add(
          await http.MultipartFile.fromPath('images', (c['file'] as File).path),
        );
        final dynamic rawAngles = c['angles'];
        if (rawAngles is Map) {
          for (final entry in rawAngles.entries) {
            final angKey = entry.key.toString();
            final angFile = entry.value;
            if (angFile is File) {
              request.files.add(
                await http.MultipartFile.fromPath('angle_images', angFile.path),
              );
              angleMetaList.add({
                "char_index": i,
                "char_name": c['name'].toString(),
                "angle": angKey,
              });
            }
          }
        }
      }
      if (angleMetaList.isNotEmpty) {
        request.fields['angle_meta'] = jsonEncode(angleMetaList);
      }

      if (customBgFile != null) {
        request.files.add(
          await http.MultipartFile.fromPath('custom_bg', customBgFile!.path),
        );
      }

      var streamedRes =
          await request.send().timeout(const Duration(seconds: 45));
      var res = await http.Response.fromStream(streamedRes);

      if (res.statusCode == 200) {
        var data = jsonDecode(res.body);
        final prefs = await SharedPreferences.getInstance();
        await prefs.setString('active_project_id', data['project_id']);

        setState(() {
          if (data['remaining_credits'] != null) {
            userCredits = data['remaining_credits'] as int;
          }
          if (data['remaining_wallet_balance'] != null) {
            walletBalance = data['remaining_wallet_balance'] as int;
          }
          activeProjectId = data['project_id'];
          statusMessage = data['status_text'] ?? "";
        });
        _startAutoSync();
      } else if (res.statusCode == 402) {
        _showBuyCreditDialog();
      } else {
        setState(
          () => statusMessage =
              "सर्वर एरर (${res.statusCode}): कृपया पुनः प्रयास करें।",
        );
      }
    } catch (e) {
      setState(
        () => statusMessage =
            "कनेक्शन फेल: कृपया ऊपर ⚙️ से सही Server URL चेक करें।",
      );
    } finally {
      setState(() => isSubmitting = false);
    }
  }

  void _startAutoSync() {
    syncTimer?.cancel();
    // Auto-poll every 3 seconds so users see smooth live progress (5% -> 25% -> 60% -> 100%)
    syncTimer = Timer.periodic(const Duration(seconds: 3), (_) async {
      if (activeProjectId == null) return;
      try {
        final res = await http
            .get(Uri.parse('$serverUrl/sync-project/$activeProjectId'))
            .timeout(const Duration(seconds: 6));
        if (res.statusCode == 200 && mounted) {
          final data = jsonDecode(res.body);
          setState(() {
            isServerOnline = true;
            progress = (data['progress'] ?? 0) as int;
            statusMessage = (data['status_text'] ?? "").toString();
            readyParts = (data['ready_parts'] ?? []) as List<dynamic>;
            scriptsData = (data['scripts'] ?? {}) as Map<String, dynamic>;
            isCompleted = (data['is_completed'] ?? false) as bool;
            if (data['aspect_ratio'] != null) {
              activeAspectRatio = data['aspect_ratio'].toString();
            }
            if (data['series_mode'] != null) {
              activeSeriesMode = data['series_mode'].toString();
            }
            if (data['total_parts'] is int) {
              activeTotalParts = data['total_parts'] as int;
            }
          });
          if (isCompleted) syncTimer?.cancel();
        }
      } catch (_) {
        if (mounted) {
          setState(
            () => statusMessage =
                "ऑफलाइन मोड: क्लाउड पर मूवी बन रही है। नेट ऑन होते ही अपडेट दिखेगा...",
          );
        }
      }
    });
  }

  void _onVideoPlayerTick() {
    final ctrl = _videoController;
    final playing = currentlyPlayingType;
    if (ctrl == null || playing == null || _autoNextTriggered) return;
    final val = ctrl.value;
    if (!val.isInitialized || val.duration <= const Duration(seconds: 1)) return;
    if (val.position >= val.duration - const Duration(milliseconds: 450)) {
      if (playing.startsWith("PART_")) {
        final int? currPart = int.tryParse(playing.replaceFirst("PART_", ""));
        if (currPart != null && readyParts.contains(currPart + 1)) {
          _autoNextTriggered = true;
          final nextPart = currPart + 1;
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              SnackBar(
                duration: const Duration(seconds: 2),
                content: Text("⏭️ Auto-Next: Part $nextPart शुरू हो रहा है..."),
              ),
            );
          }
          Future.microtask(() => _playOnlinePreview("PART_$nextPart"));
        }
      }
    }
  }

  Future<void> _playOnlinePreview(String fileType) async {
    try {
      final streamUrl = "$serverUrl/stream/$activeProjectId/$fileType";
      _autoNextTriggered = false;
      _videoController?.removeListener(_onVideoPlayerTick);
      _videoController?.dispose();
      _chewieController?.dispose();

      _videoController = VideoPlayerController.networkUrl(Uri.parse(streamUrl));
      await _videoController!.initialize();
      _videoController!.addListener(_onVideoPlayerTick);
      final double fallbackRatio =
          activeAspectRatio == "9:16" ? (9 / 16) : (16 / 9);
      final double vidRatio = _videoController!.value.aspectRatio > 0
          ? _videoController!.value.aspectRatio
          : fallbackRatio;
      _chewieController = ChewieController(
        videoPlayerController: _videoController!,
        aspectRatio: vidRatio,
        autoPlay: true,
        looping: false,
      );
      setState(() => currentlyPlayingType = fileType);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("वीडियो लोड करने में समस्या: $e")),
        );
      }
    }
  }

  Future<void> _openCustomizeSceneDialog() async {
    if (scriptsData.isEmpty) return;
    String selectedChapter = scriptsData.keys.first;
    int selectedIndex = 0;
    String chosenLocKey = "mumbai_rain";
    String chosenMood = "Action";
    String chosenSfx = "none";
    TextEditingController locNameCtrl =
        TextEditingController(text: "Mumbai ki Barish");
    TextEditingController actionPromptCtrl = TextEditingController(
      text: "character running through stormy street, fast tracking camera, sparks flying",
    );
    File? pickedBg;

    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setModalState) {
          List dialogues = scriptsData[selectedChapter] ?? [];
          return AlertDialog(
            backgroundColor: const Color(0xFF1B1236),
            title: const Text(
              "🎨 सीन लोकेशन / म्यूज़िक / SFX / BG",
              style: TextStyle(color: Color(0xFF00E5FF), fontSize: 16),
            ),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  DropdownButtonFormField<String>(
                    value: selectedChapter,
                    items: scriptsData.keys
                        .map(
                          (k) => DropdownMenuItem(
                            value: k,
                            child: Text("चैप्टर $k"),
                          ),
                        )
                        .toList(),
                    onChanged: (val) => setModalState(() {
                      selectedChapter = val!;
                      selectedIndex = 0;
                    }),
                  ),
                  const SizedBox(height: 8),
                  if (dialogues.isNotEmpty)
                    DropdownButtonFormField<int>(
                      value: selectedIndex,
                      isExpanded: true,
                      items: List.generate(
                        dialogues.length,
                        (i) => DropdownMenuItem(
                          value: i,
                          child: Text(
                            "${i + 1}. ${dialogues[i]['character']}: ${dialogues[i]['dialogue']}",
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ),
                      onChanged: (val) =>
                          setModalState(() => selectedIndex = val!),
                    ),
                  const SizedBox(height: 10),
                  DropdownButtonFormField<String>(
                    value: chosenLocKey,
                    decoration: const InputDecoration(
                      labelText: "Real Cinematic Location",
                      border: OutlineInputBorder(),
                    ),
                    items: _locationPresets
                        .map(
                          (p) => DropdownMenuItem(
                            value: p['key'],
                            child: Text(p['label']!),
                          ),
                        )
                        .toList(),
                    onChanged: (val) {
                      final match = _locationPresets
                          .firstWhere((e) => e['key'] == val);
                      setModalState(() {
                        chosenLocKey = val!;
                        locNameCtrl.text = match['label']!;
                      });
                    },
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    controller: locNameCtrl,
                    decoration: const InputDecoration(
                      labelText: "Location Name",
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 8),
                  DropdownButtonFormField<String>(
                    value: chosenMood,
                    decoration: const InputDecoration(
                      labelText: "Background Music / Mood (20% Vol)",
                      border: OutlineInputBorder(),
                    ),
                    items: _moods
                        .map(
                          (m) => DropdownMenuItem(value: m, child: Text(m)),
                        )
                        .toList(),
                    onChanged: (val) => setModalState(() => chosenMood = val!),
                  ),
                  const SizedBox(height: 8),
                  DropdownButtonFormField<String>(
                    value: chosenSfx,
                    decoration: const InputDecoration(
                      labelText: "Foley Sound Effects (SFX)",
                      border: OutlineInputBorder(),
                    ),
                    items: _sfxOptions
                        .map(
                          (s) => DropdownMenuItem(
                            value: s,
                            child: Text(s.toUpperCase()),
                          ),
                        )
                        .toList(),
                    onChanged: (val) => setModalState(() => chosenSfx = val!),
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    controller: actionPromptCtrl,
                    decoration: const InputDecoration(
                      labelText: "AI Motion Prompt (LTX-Video / Pro VFX)",
                      hintText: "e.g. hero charging in rain, camera tracking",
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 10),
                  OutlinedButton.icon(
                    onPressed: () async {
                      final picker = ImagePicker();
                      final XFile? photo = await picker.pickImage(
                        source: ImageSource.gallery,
                        imageQuality: 85,
                      );
                      if (photo != null) {
                        setModalState(() => pickedBg = File(photo.path));
                      }
                    },
                    icon: const Icon(Icons.image, color: Color(0xFFFFD600)),
                    label: Text(
                      pickedBg != null
                          ? "Custom BG Selected ✓"
                          : "Custom BG Photo चुनें (Optional)",
                      style: const TextStyle(color: Colors.white),
                    ),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx),
                child: const Text("रद्द करें"),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF00E5FF),
                ),
                onPressed: () async {
                  Navigator.pop(ctx);
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text("सीन लोकेशन, SFX और बैकग्राउंड रेंडर हो रहा है..."),
                    ),
                  );
                  var req = http.MultipartRequest(
                    'POST',
                    Uri.parse('$serverUrl/customize-scene/$activeProjectId'),
                  );
                  req.fields['chapter_no'] = selectedChapter;
                  req.fields['dialogue_index'] = selectedIndex.toString();
                  req.fields['location_key'] = chosenLocKey;
                  req.fields['location'] = locNameCtrl.text.trim();
                  req.fields['mood'] = chosenMood;
                  req.fields['sfx'] = chosenSfx;
                  if (actionPromptCtrl.text.trim().isNotEmpty) {
                    req.fields['visual_action_prompt'] =
                        actionPromptCtrl.text.trim();
                  }
                  if (pickedBg != null) {
                    req.files.add(
                      await http.MultipartFile.fromPath(
                        'bg_image',
                        pickedBg!.path,
                      ),
                    );
                  }
                  final streamed = await req.send();
                  if (streamed.statusCode == 200 && mounted) {
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text("✅ सीन अपडेट हो गया! अब दोबारा Play करें।"),
                      ),
                    );
                    _startAutoSync();
                  }
                },
                child: const Text(
                  "Apply करें",
                  style: TextStyle(
                    color: Colors.black,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  Future<void> _openEditDialogueDialog() async {
    if (scriptsData.isEmpty) return;
    String selectedChapter = scriptsData.keys.first;
    int selectedIndex = 0;
    TextEditingController editInstructionCtrl = TextEditingController();

    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setModalState) {
          List dialogues = scriptsData[selectedChapter] ?? [];
          String currentLine = dialogues.isNotEmpty
              ? "${dialogues[selectedIndex]['character']}: ${dialogues[selectedIndex]['dialogue']}"
              : "";

          return AlertDialog(
            backgroundColor: const Color(0xFF1B1236),
            title: const Text(
              "✂️ डायलॉग बदलें (AI रिप्लेस)",
              style: TextStyle(color: Color(0xFFFFD600)),
            ),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  DropdownButtonFormField<String>(
                    value: selectedChapter,
                    items: scriptsData.keys
                        .map(
                          (k) => DropdownMenuItem(
                            value: k,
                            child: Text("चैप्टर $k"),
                          ),
                        )
                        .toList(),
                    onChanged: (val) => setModalState(() {
                      selectedChapter = val!;
                      selectedIndex = 0;
                    }),
                  ),
                  const SizedBox(height: 8),
                  if (dialogues.isNotEmpty)
                    DropdownButtonFormField<int>(
                      value: selectedIndex,
                      isExpanded: true,
                      items: List.generate(
                        dialogues.length,
                        (i) => DropdownMenuItem(
                          value: i,
                          child: Text(
                            "${i + 1}. ${dialogues[i]['character']}: ${dialogues[i]['dialogue']}",
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ),
                      onChanged: (val) =>
                          setModalState(() => selectedIndex = val!),
                    ),
                  const SizedBox(height: 8),
                  Text(
                    "मौजूदा लाइन: $currentLine",
                    style: const TextStyle(fontSize: 12, color: Colors.white70),
                  ),
                  const SizedBox(height: 10),
                  TextField(
                    controller: editInstructionCtrl,
                    decoration: const InputDecoration(
                      border: OutlineInputBorder(),
                      labelText: "क्या बदलाव करना है?",
                      hintText: "जैसे: इसे और ज़्यादा फनी बना दो",
                    ),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx),
                child: const Text("रद्द करें"),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFFF007F),
                ),
                onPressed: () async {
                  Navigator.pop(ctx);
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text("AI सिर्फ इसी डायलॉग को बदल रहा है..."),
                    ),
                  );
                  final res = await http.post(
                    Uri.parse('$serverUrl/edit-scene/$activeProjectId'),
                    body: {
                      "chapter_no": selectedChapter,
                      "dialogue_index": selectedIndex.toString(),
                      "instruction": editInstructionCtrl.text,
                    },
                  );
                  if (res.statusCode == 200 && mounted) {
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text(
                          "✅ डायलॉग बदल गया! अब दोबारा Play करके चेक करें।",
                        ),
                      ),
                    );
                    _startAutoSync();
                  }
                },
                child: const Text(
                  "बदलें",
                  style: TextStyle(color: Colors.white),
                ),
              )
            ],
          );
        },
      ),
    );
  }

  Future<String> _saveDownloadedFileToGallery(
    File tempFile,
    String fileName,
  ) async {
    final lower = fileName.toLowerCase();
    final bool isImage = lower.endsWith(".jpg") ||
        lower.endsWith(".jpeg") ||
        lower.endsWith(".png") ||
        lower.endsWith(".webp");
    try {
      bool hasAccess = await Gal.hasAccess(toAlbum: true);
      if (!hasAccess) {
        hasAccess = await Gal.requestAccess(toAlbum: true);
      }
      if (hasAccess) {
        if (isImage) {
          await Gal.putImage(tempFile.path, album: 'CinePopAI');
          return "Phone Gallery (Pictures/CinePopAI)";
        } else {
          await Gal.putVideo(tempFile.path, album: 'CinePopAI');
          return "Phone Gallery (Movies/CinePopAI)";
        }
      }
    } catch (_) {}

    // Fallback: save directly into public Movies/CinePopAI or app storage
    try {
      final pubDir = Directory(
        isImage
            ? "/storage/emulated/0/Pictures/CinePopAI"
            : "/storage/emulated/0/Movies/CinePopAI",
      );
      if (!await pubDir.exists()) {
        await pubDir.create(recursive: true);
      }
      final dest = "${pubDir.path}/$fileName";
      await tempFile.copy(dest);
      return isImage ? "Pictures/CinePopAI" : "Movies/CinePopAI";
    } catch (_) {
      final fallbackDir = await getExternalStorageDirectory() ??
          await getApplicationDocumentsDirectory();
      final dest = "${fallbackDir.path}/$fileName";
      await tempFile.copy(dest);
      return dest;
    }
  }

  Future<void> _downloadVideo(String fileType) async {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text("$fileType Phone Gallery में डाउनलोड हो रहा है...")),
    );
    try {
      final tempDir = await getTemporaryDirectory();
      final fileName = "CinePop_${activeProjectId}_$fileType.mp4";
      final tempFile = File("${tempDir.path}/$fileName");
      final sink = tempFile.openWrite();

      final request = http.Request(
        'GET',
        Uri.parse('$serverUrl/download/$activeProjectId/$fileType'),
      );
      final response = await request.send();

      if (response.statusCode == 200) {
        await for (var chunk in response.stream) {
          sink.add(chunk);
        }
        await sink.close();
        final savedLocation =
            await _saveDownloadedFileToGallery(tempFile, fileName);
        try {
          if (await tempFile.exists()) await tempFile.delete();
        } catch (_) {}
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              backgroundColor: Colors.green.shade700,
              content: Text("✅ $savedLocation में सेव हो गया!"),
            ),
          );
        }
      } else {
        await sink.close();
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text("डाउनलोड फेल (${response.statusCode})")),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("डाउनलोड एरर: $e")),
        );
      }
    }
  }

  Future<void> _downloadDirectUrl(String url, String fileName) async {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text("$fileName Phone Gallery में डाउनलोड हो रहा है...")),
    );
    try {
      final tempDir = await getTemporaryDirectory();
      final tempFile = File("${tempDir.path}/$fileName");
      final sink = tempFile.openWrite();
      final response = await http.Request('GET', Uri.parse(url)).send();
      if (response.statusCode == 200) {
        await for (var chunk in response.stream) {
          sink.add(chunk);
        }
        await sink.close();
        final savedLocation =
            await _saveDownloadedFileToGallery(tempFile, fileName);
        try {
          if (await tempFile.exists()) await tempFile.delete();
        } catch (_) {}
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              backgroundColor: Colors.green.shade700,
              content: Text("✅ $savedLocation में सेव हो गया!"),
            ),
          );
        }
      } else {
        await sink.close();
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("डाउनलोड एरर: $e")),
        );
      }
    }
  }

  Future<void> _playCustomStreamUrl(String url, String label) async {
    try {
      _videoController?.dispose();
      _chewieController?.dispose();
      _videoController = VideoPlayerController.networkUrl(Uri.parse(url));
      await _videoController!.initialize();
      _chewieController = ChewieController(
        videoPlayerController: _videoController!,
        autoPlay: true,
        looping: false,
      );
      setState(() => currentlyPlayingType = label);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("स्ट्रीम लोड एरर: $e")),
        );
      }
    }
  }

  Future<void> _generateTrailerAndReel() async {
    if (activeProjectId == null || generatingTrailer) return;
    setState(() => generatingTrailer = true);
    try {
      final res = await http
          .post(Uri.parse('$serverUrl/generate-trailer/$activeProjectId'))
          .timeout(const Duration(seconds: 90));
      if (res.statusCode == 200 && mounted) {
        await showDialog(
          context: context,
          builder: (ctx) => AlertDialog(
            backgroundColor: const Color(0xFF1B1236),
            title: const Text(
              "🎬 60-Sec Viral Trailer & 9:16 Reel",
              style: TextStyle(color: Color(0xFFFFD600), fontSize: 16),
            ),
            content: const Text(
              "आपकी मूवी के बेस्ट एक्शन और डायलॉग सीन से 16:9 YouTube Trailer और 9:16 Vertical Instagram Reel दोनों तैयार हैं!",
              style: TextStyle(fontSize: 13, color: Colors.white70),
            ),
            actions: [
              TextButton(
                onPressed: () {
                  Navigator.pop(ctx);
                  _playCustomStreamUrl(
                    '$serverUrl/stream-trailer/$activeProjectId?format=16x9',
                    '16:9 Trailer',
                  );
                },
                child: const Text("▶ 16:9 Play"),
              ),
              TextButton(
                onPressed: () {
                  Navigator.pop(ctx);
                  _downloadDirectUrl(
                    '$serverUrl/download-trailer/$activeProjectId?format=16x9',
                    'CinePop_Trailer_16x9_$activeProjectId.mp4',
                  );
                },
                child: const Text("⬇ 16:9 MP4"),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFFF007F),
                ),
                onPressed: () {
                  Navigator.pop(ctx);
                  _downloadDirectUrl(
                    '$serverUrl/download-trailer/$activeProjectId?format=9x16',
                    'CinePop_Reel_9x16_$activeProjectId.mp4',
                  );
                },
                child: const Text(
                  "⬇ 9:16 Reel",
                  style: TextStyle(color: Colors.white),
                ),
              ),
            ],
          ),
        );
      } else if (mounted) {
        final d = jsonDecode(res.body);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(d['detail'] ?? "ट्रेलर बनाने में समस्या आई।")),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("Trailer एरर: $e")),
        );
      }
    } finally {
      if (mounted) setState(() => generatingTrailer = false);
    }
  }

  Future<void> _generateAndShowPoster(String projectId) async {
    setState(() => generatingPoster = true);
    try {
      final res = await http
          .post(Uri.parse('$serverUrl/generate-poster/$projectId'))
          .timeout(const Duration(seconds: 30));
      if (res.statusCode == 200 && mounted) {
        final posterUrl =
            '$serverUrl/poster/$projectId?t=${DateTime.now().millisecondsSinceEpoch}';
        await showDialog(
          context: context,
          builder: (ctx) => AlertDialog(
            backgroundColor: const Color(0xFF1B1236),
            title: const Text(
              "🖼️ AI Bollywood Movie Poster",
              style: TextStyle(color: Color(0xFFFFD600), fontSize: 16),
            ),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  ClipRRect(
                    borderRadius: BorderRadius.circular(14),
                    child: Image.network(posterUrl, fit: BoxFit.contain),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx),
                child: const Text("बंद करें"),
              ),
              ElevatedButton.icon(
                style: ElevatedButton.styleFrom(backgroundColor: Colors.green),
                onPressed: () {
                  Navigator.pop(ctx);
                  _downloadDirectUrl(
                    '$serverUrl/poster/$projectId?download=1',
                    'CinePop_Poster_$projectId.jpg',
                  );
                },
                icon: const Icon(Icons.download, color: Colors.white, size: 18),
                label: const Text(
                  "Download HD Poster",
                  style: TextStyle(color: Colors.white),
                ),
              ),
            ],
          ),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("Poster एरर: $e")),
        );
      }
    } finally {
      if (mounted) setState(() => generatingPoster = false);
    }
  }

  Future<void> _openDubLanguageDialog() async {
    if (activeProjectId == null) return;
    String targetLang = "Bhojpuri";
    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setModalState) => AlertDialog(
          backgroundColor: const Color(0xFF1B1236),
          title: const Text(
            "🗣️ Multi-Language Dubbing",
            style: TextStyle(color: Color(0xFF00E5FF), fontSize: 17),
          ),
          content: DropdownButtonFormField<String>(
            value: targetLang,
            dropdownColor: const Color(0xFF1B1236),
            decoration: const InputDecoration(
              labelText: "नई भाषा चुनें",
              border: OutlineInputBorder(),
            ),
            items: _languages
                .map((l) => DropdownMenuItem(value: l, child: Text(l)))
                .toList(),
            onChanged: (v) => setModalState(() => targetLang = v!),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: const Text("रद्द करें"),
            ),
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFFFF007F),
              ),
              onPressed: () async {
                Navigator.pop(ctx);
                setState(() => dubbingMovie = true);
                try {
                  final res = await http.post(
                    Uri.parse('$serverUrl/dub-language/$activeProjectId'),
                    body: {"target_language": targetLang},
                  );
                  if (res.statusCode == 200 && mounted) {
                    final d = jsonDecode(res.body);
                    ScaffoldMessenger.of(context).showSnackBar(
                      SnackBar(content: Text(d['message'] ?? "Dubbing started")),
                    );
                    _startAutoSync();
                  }
                } finally {
                  if (mounted) setState(() => dubbingMovie = false);
                }
              },
              child: const Text(
                "Dub शुरू करें",
                style: TextStyle(color: Colors.white),
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ==================== FACE-SWAP METHODS ====================

  Future<void> _pickFaceSwapVideo() async {
    final picker = ImagePicker();
    final XFile? vid = await picker.pickVideo(source: ImageSource.gallery);
    if (vid != null) {
      setState(() => fsVideoFile = File(vid.path));
    }
  }

  Future<void> _pickFaceSwapFace() async {
    final picker = ImagePicker();
    final XFile? img =
        await picker.pickImage(source: ImageSource.gallery, imageQuality: 90);
    if (img != null) {
      setState(() => fsFaceFile = File(img.path));
    }
  }

  Future<void> _startFaceSwapJob() async {
    if (fsSubmitting || userEmail == null) return;
    if (fsVideoFile == null || fsFaceFile == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text("कृपया वीडियो क्लिप और अपना फेस फोटो दोनों चुनें!"),
        ),
      );
      return;
    }

    setState(() {
      fsSubmitting = true;
      fsStatusText = "वीडियो और फेस फोटो क्लाउड AI Face-Swap इंजन पर जा रहे हैं...";
      fsStatus = "processing";
    });

    try {
      final req = http.MultipartRequest(
        'POST',
        Uri.parse('$serverUrl/face-swap/start/'),
      );
      req.fields['user_email'] = userEmail!;
      req.fields['title'] = fsTitleController.text.trim().isEmpty
          ? "Custom Face-Swap"
          : fsTitleController.text.trim();
      req.fields['dub_text'] = fsDubController.text.trim();
      req.fields['dub_language'] = fsDubLanguage;
      req.fields['dub_pitch'] = fsDubPitch;
      req.fields['watermark_text'] = watermarkController.text.trim();
      req.files.add(
        await http.MultipartFile.fromPath('video', fsVideoFile!.path),
      );
      req.files.add(
        await http.MultipartFile.fromPath('face_image', fsFaceFile!.path),
      );

      final streamed = await req.send().timeout(const Duration(seconds: 60));
      final res = await http.Response.fromStream(streamed);
      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        setState(() {
          fsActiveJobId = data['job_id']?.toString();
          fsStatus = data['status']?.toString() ?? "processing";
          fsStatusText = data['status_text']?.toString() ?? "";
        });
        _startFaceSwapPolling();
      } else {
        setState(() {
          fsStatus = "failed";
          fsStatusText = "सर्वर एरर (${res.statusCode}) — वीडियो 65MB से कम रखें।";
        });
      }
    } catch (e) {
      setState(() {
        fsStatus = "failed";
        fsStatusText = "कनेक्शन एरर: $e";
      });
    } finally {
      if (mounted) setState(() => fsSubmitting = false);
    }
  }

  void _startFaceSwapPolling() {
    fsPollTimer?.cancel();
    fsPollTimer = Timer.periodic(const Duration(seconds: 4), (_) async {
      if (fsActiveJobId == null) return;
      try {
        final res = await http
            .get(Uri.parse('$serverUrl/face-swap/status/$fsActiveJobId'))
            .timeout(const Duration(seconds: 6));
        if (res.statusCode == 200 && mounted) {
          final data = jsonDecode(res.body);
          final st = (data['status'] ?? "").toString();
          setState(() {
            fsStatus = st;
            fsStatusText = (data['status_text'] ?? "").toString();
          });
          if (st == "ready") {
            fsPollTimer?.cancel();
            await _playFaceSwapPreview(fsActiveJobId!);
          } else if (st == "failed") {
            fsPollTimer?.cancel();
          }
        }
      } catch (_) {}
    });
  }

  Future<void> _playFaceSwapPreview(String jobId) async {
    try {
      _fsVideoController?.dispose();
      _fsChewieController?.dispose();
      _fsVideoController = VideoPlayerController.networkUrl(
        Uri.parse('$serverUrl/face-swap/stream/$jobId'),
      );
      await _fsVideoController!.initialize();
      _fsChewieController = ChewieController(
        videoPlayerController: _fsVideoController!,
        autoPlay: true,
        looping: false,
      );
      setState(() => fsActiveJobId = jobId);
    } catch (_) {}
  }

  // ==================== CLOUD LIBRARY & REFER METHODS ====================

  Future<void> _fetchCloudLibrary() async {
    if (userEmail == null) return;
    setState(() => loadingLibrary = true);
    try {
      final res = await http
          .get(
            Uri.parse(
              '$serverUrl/my-library/?user_email=${Uri.encodeComponent(userEmail!)}',
            ),
          )
          .timeout(const Duration(seconds: 10));
      if (res.statusCode == 200 && mounted) {
        final data = jsonDecode(res.body);
        setState(() {
          libraryMovies = data['movies'] ?? [];
          libraryFaceSwaps = data['face_swaps'] ?? [];
        });
      }
    } catch (_) {}
    if (mounted) setState(() => loadingLibrary = false);
  }

  Future<void> _fetchReferralSummary() async {
    if (userEmail == null) return;
    try {
      final res = await http
          .get(
            Uri.parse(
              '$serverUrl/referral/summary/?email=${Uri.encodeComponent(userEmail!)}',
            ),
          )
          .timeout(const Duration(seconds: 8));
      if (res.statusCode == 200 && mounted) {
        final data = jsonDecode(res.body);
        setState(() {
          myReferralCode = (data['referral_code'] ?? myReferralCode).toString();
          referredBy = (data['referred_by'] ?? referredBy).toString();
          referralsCount = (data['referrals_count'] ?? referralsCount) as int;
          referralEarnings =
              (data['referral_earnings'] ?? referralEarnings) as int;
          referralHistory = data['history'] ?? [];
        });
      }
    } catch (_) {}
  }

  Future<void> _applyFriendReferralCode() async {
    final code = applyReferralController.text.trim().toUpperCase();
    if (code.isEmpty || userEmail == null) return;
    try {
      final res = await http.post(
        Uri.parse('$serverUrl/referral/apply/'),
        body: {"email": userEmail!, "code": code},
      );
      final data = jsonDecode(res.body);
      if (res.statusCode == 200 && mounted) {
        applyReferralController.clear();
        await _refreshUserBalance();
        await _fetchReferralSummary();
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(data['message'] ?? "✅ Referral Applied!")),
        );
      } else if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(data['detail'] ?? "Invalid Referral Code")),
        );
      }
    } catch (_) {}
  }

  @override
  Widget build(BuildContext context) {
    if (userEmail == null) {
      return Scaffold(
        body: Container(
          decoration: const BoxDecoration(
            gradient: LinearGradient(
              colors: [Color(0xFF1A0933), Color(0xFF0E091D), Color(0xFF2B0938)],
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
            ),
          ),
          child: Center(
            child: Padding(
              padding: const EdgeInsets.all(24.0),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  ClipRRect(
                    borderRadius: BorderRadius.circular(32),
                    child: Image.asset('assets/logo.png', width: 140, height: 140),
                  ),
                  const SizedBox(height: 20),
                  const Text(
                    "CinePop AI 🎬",
                    style: TextStyle(
                      fontSize: 34,
                      fontWeight: FontWeight.w900,
                      color: Color(0xFFFFD600),
                      shadows: [Shadow(color: Color(0xFFFF007F), blurRadius: 16)],
                    ),
                  ),
                  const SizedBox(height: 8),
                  const Text(
                    "फोटो डालो, कहानी बताओ — रियल सिनेमैटिक लोकेशन के साथ पूरी मूवी तैयार!\nसिर्फ ₹50 प्रति मूवी",
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 15, color: Color(0xFF00E5FF)),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Icon(
                        Icons.circle,
                        size: 10,
                        color: isServerOnline
                            ? Colors.greenAccent
                            : Colors.redAccent,
                      ),
                      const SizedBox(width: 6),
                      Text(
                        isServerOnline
                            ? "Server Online"
                            : "Server Offline (Settings में Railway URL डालें)",
                        style: const TextStyle(
                          fontSize: 12,
                          color: Colors.white70,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 30),
                  SizedBox(
                    width: double.infinity,
                    child: ElevatedButton.icon(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: Colors.white,
                        foregroundColor: Colors.black,
                        padding: const EdgeInsets.symmetric(
                          horizontal: 24,
                          vertical: 16,
                        ),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(30),
                        ),
                      ),
                      onPressed: _handleGoogleSignIn,
                      icon: const Icon(Icons.login, color: Color(0xFFFF007F)),
                      label: const Text(
                        "Continue with Google",
                        style: TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 12),
                  SizedBox(
                    width: double.infinity,
                    child: ElevatedButton.icon(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF00E5FF),
                        foregroundColor: const Color(0xFF0E091D),
                        padding: const EdgeInsets.symmetric(
                          horizontal: 24,
                          vertical: 16,
                        ),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(30),
                        ),
                      ),
                      onPressed:
                          isInstantLoggingIn ? null : _handleInstantGuestLogin,
                      icon: const Icon(Icons.bolt, color: Color(0xFF0E091D)),
                      label: Text(
                        isInstantLoggingIn
                            ? "तुरंत लॉगिन हो रहा है..."
                            : "⚡ 1-Click Guest / Instant Start",
                        style: const TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w900,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                  const Text(
                    "नए यूज़र को 1 Free Trial Video Pass तुरंत मिलेगा!",
                    style: TextStyle(
                      fontSize: 12,
                      color: Color(0xFFFFD600),
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextButton.icon(
                    onPressed: _configureServerUrl,
                    icon: const Icon(Icons.settings, color: Colors.white54),
                    label: const Text(
                      "Server Setup",
                      style: TextStyle(color: Colors.white54),
                    ),
                  )
                ],
              ),
            ),
          ),
        ),
      );
    }

    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF1A0E36),
        title: Row(
          children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(8),
              child: Image.asset('assets/logo.png', width: 30, height: 30),
            ),
            const SizedBox(width: 8),
            const Text(
              "CinePop AI",
              style: TextStyle(
                fontSize: 18,
                fontWeight: FontWeight.w900,
                color: Color(0xFFFFD600),
              ),
            ),
          ],
        ),
        actions: [
          GestureDetector(
            onTap: _showBuyCreditDialog,
            child: Container(
              margin: const EdgeInsets.symmetric(vertical: 10),
              padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
              decoration: BoxDecoration(
                gradient: const LinearGradient(
                  colors: [Color(0xFFFF007F), Color(0xFF7000FF)],
                ),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Center(
                child: Text(
                  isAdmin
                      ? "👑 ADMIN FREE"
                      : "₹$walletBalance | 🎟️$userCredits",
                  style: const TextStyle(
                    fontWeight: FontWeight.bold,
                    fontSize: 11,
                  ),
                ),
              ),
            ),
          ),
          if (isAdminEmail || isAdmin)
            IconButton(
              icon: Icon(
                isAdmin ? Icons.admin_panel_settings : Icons.lock,
                color: const Color(0xFF00E5FF),
              ),
              tooltip: "Admin Dashboard (PIN Lock)",
              onPressed: _openAdminPanelWithPinLock,
            ),
          IconButton(
            icon: const Icon(Icons.settings),
            onPressed: _configureServerUrl,
          ),
          IconButton(
            icon: const Icon(Icons.logout, color: Colors.redAccent),
            onPressed: _logout,
          ),
        ],
      ),
      body: Padding(
        padding: const EdgeInsets.all(16.0),
        child: currentTab == 0
            ? (activeProjectId != null
                ? _buildStudioScreen()
                : _buildSetupScreen())
            : currentTab == 1
                ? _buildFaceSwapScreen()
                : currentTab == 2
                    ? _buildLibraryScreen()
                    : _buildReferEarnScreen(),
      ),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: currentTab,
        type: BottomNavigationBarType.fixed,
        backgroundColor: const Color(0xFF160B2E),
        selectedItemColor: const Color(0xFFFFD600),
        unselectedItemColor: Colors.white60,
        onTap: (idx) {
          setState(() => currentTab = idx);
          if (idx == 2) _fetchCloudLibrary();
          if (idx == 3) _fetchReferralSummary();
        },
        items: const [
          BottomNavigationBarItem(
            icon: Icon(Icons.movie_creation),
            label: "Studio",
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.face_retouching_natural),
            label: "Face-Swap",
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.video_library),
            label: "My Movies",
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.card_giftcard),
            label: "Refer",
          ),
        ],
      ),
    );
  }

  Widget _buildSetupScreen() {
    return ListView(
      children: [
        // User Wallet & Pass Header Card
        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            gradient: const LinearGradient(
              colors: [Color(0xFF2B125A), Color(0xFF180C36)],
            ),
            borderRadius: BorderRadius.circular(16),
            border: Border.all(
              color: const Color(0xFFFF007F).withOpacity(0.5),
            ),
          ),
          child: Row(
            children: [
              ClipOval(
                child: SizedBox(
                  width: 44,
                  height: 44,
                  child: (userPhoto != null && userPhoto!.isNotEmpty)
                      ? Image.network(
                          userPhoto!,
                          fit: BoxFit.cover,
                          alignment: Alignment.topCenter,
                          errorBuilder: (_, __, ___) => Container(
                            color: const Color(0xFF3A1D6E),
                            child: const Icon(Icons.person, color: Colors.white),
                          ),
                        )
                      : (characters.isNotEmpty && characters.first['file'] is File)
                          ? Image.file(
                              characters.first['file'] as File,
                              fit: BoxFit.cover,
                              alignment: Alignment.topCenter,
                            )
                          : Container(
                              color: const Color(0xFF3A1D6E),
                              child: const Icon(Icons.person, color: Colors.white),
                            ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      "नमस्ते, $userName!",
                      style: const TextStyle(fontWeight: FontWeight.bold),
                    ),
                    Text(
                      isAdmin
                          ? "Admin PIN Verified · Free Unlimited"
                          : "वॉलेट: ₹$walletBalance · मूवी पास: $userCredits",
                      style: const TextStyle(
                        fontSize: 12,
                        color: Colors.greenAccent,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
              IconButton(
                icon: const Icon(Icons.refresh, color: Color(0xFF00E5FF)),
                tooltip: "Sync Balance",
                onPressed: _refreshUserBalance,
              ),
              if (!isAdmin)
                ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFFFFD600),
                    padding: const EdgeInsets.symmetric(horizontal: 10),
                  ),
                  onPressed: _showBuyCreditDialog,
                  child: const Text(
                    "₹50 / Promo",
                    style: TextStyle(
                      color: Colors.black,
                      fontWeight: FontWeight.bold,
                      fontSize: 12,
                    ),
                  ),
                )
            ],
          ),
        ),
        const SizedBox(height: 14),

        // ⚡ 1-Click Quick Templates Chips (Beginner-Friendly Auto-Fill)
        Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: const Color(0xFF1A1033),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(
              color: const Color(0xFFFFD600).withOpacity(0.45),
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                "⚡ 1-Click Quick Templates (टैप करें — कहानी और जॉनर खुद भर जाएगा!)",
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.bold,
                  color: Color(0xFFFFD600),
                ),
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 6,
                children: [
                  {
                    "label": "🔥 Action Hero",
                    "genre": "Action",
                    "mood": "Action",
                    "loc": "Mumbai ki Barish",
                    "topic":
                        "मुंबई की भारी बारिश में एक जांबाज हीरो अकेले अंडरवर्ल्ड डॉन के अड्डे पर हमला कर शहर को बचाता है",
                  },
                  {
                    "label": "💰 Ghar Jamai Twist",
                    "genre": "Thriller",
                    "mood": "Suspense",
                    "loc": "Royal Palace",
                    "topic":
                        "ससुराल में जिसे सब गरीब घर जमाई समझकर अपमानित करते थे, शादी की सालगिरह पर अचानक पता चलता है कि वो पूरे शहर का सबसे बड़ा अरबपति मालिक है",
                  },
                  {
                    "label": "😂 Comedy Reel",
                    "genre": "Comedy",
                    "mood": "Comedy",
                    "loc": "Mumbai City Street",
                    "topic":
                        "दो मज़ेदार दोस्त गलत सूटकेस बदल जाने के बाद एक अजीबोगरीब डॉन और पुलिस दोनों से फनी अंदाज़ में भागते हैं",
                  },
                  {
                    "label": "👻 Horror Night",
                    "genre": "Horror",
                    "mood": "Suspense",
                    "loc": "Purani Haveli",
                    "topic":
                        "आधी रात को तूफ़ान में दो दोस्त एक 200 साल पुरानी भूतिया हवेली में फंस जाते हैं जहाँ अचानक दरवाज़े बंद हो जाते हैं",
                  },
                ].map((tpl) {
                  final active = selectedGenre == tpl['genre'] &&
                      aiTopicController.text == tpl['topic'];
                  return ActionChip(
                    backgroundColor: active
                        ? const Color(0xFFFF007F)
                        : const Color(0xFF281854),
                    label: Text(
                      tpl['label']!,
                      style: const TextStyle(
                        color: Colors.white,
                        fontWeight: FontWeight.bold,
                        fontSize: 12,
                      ),
                    ),
                    onPressed: () {
                      setState(() {
                        storyMode = "ai";
                        selectedGenre = tpl['genre']!;
                        defaultMood = tpl['mood']!;
                        defaultLocation = tpl['loc']!;
                        locationController.text = tpl['loc']!;
                        aiTopicController.text = tpl['topic']!;
                        ideaController.text = tpl['topic']!;
                      });
                      ScaffoldMessenger.of(context).showSnackBar(
                        SnackBar(
                          duration: const Duration(seconds: 2),
                          content: Text(
                            "${tpl['label']} सेट हो गया! अब 3D Face Scan करें और Start दबाएँ।",
                          ),
                        ),
                      );
                    },
                  );
                }).toList(),
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),

        // 1A. Video Platform & Aspect Ratio Selector (Instagram 9:16 vs YouTube 16:9)
        const Text(
          "1. वीडियो प्लेटफ़ॉर्म और साइज़ चुनें (Platform & Ratio):",
          style: TextStyle(
            fontWeight: FontWeight.bold,
            color: Color(0xFF00E5FF),
          ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: GestureDetector(
                onTap: () {
                  selectedAspectRatio = "9:16";
                  _updateDataEstimate();
                },
                child: Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    gradient: selectedAspectRatio == "9:16"
                        ? const LinearGradient(
                            colors: [Color(0xFF3E1148), Color(0xFF1F123A)],
                          )
                        : null,
                    color: selectedAspectRatio == "9:16"
                        ? null
                        : const Color(0xFF160F2D),
                    borderRadius: BorderRadius.circular(14),
                    border: Border.all(
                      color: selectedAspectRatio == "9:16"
                          ? const Color(0xFFFF007F)
                          : Colors.white24,
                      width: selectedAspectRatio == "9:16" ? 2 : 1,
                    ),
                  ),
                  child: Column(
                    children: [
                      Container(
                        width: 40,
                        height: 40,
                        decoration: BoxDecoration(
                          gradient: const LinearGradient(
                            colors: [
                              Color(0xFFF58529),
                              Color(0xFFDD2A7B),
                              Color(0xFF8134AF),
                            ],
                          ),
                          borderRadius: BorderRadius.circular(10),
                        ),
                        child: const Icon(
                          Icons.smartphone,
                          color: Colors.white,
                          size: 22,
                        ),
                      ),
                      const SizedBox(height: 8),
                      const Text(
                        "Instagram / Moj Reels",
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Colors.white,
                          fontWeight: FontWeight.bold,
                          fontSize: 12,
                        ),
                      ),
                      const SizedBox(height: 2),
                      const Text(
                        "9:16 Vertical · 720×1280",
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Color(0xFFFFD600),
                          fontSize: 10,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: GestureDetector(
                onTap: () {
                  selectedAspectRatio = "16:9";
                  _updateDataEstimate();
                },
                child: Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    gradient: selectedAspectRatio == "16:9"
                        ? const LinearGradient(
                            colors: [Color(0xFF3E101F), Color(0xFF1F123A)],
                          )
                        : null,
                    color: selectedAspectRatio == "16:9"
                        ? null
                        : const Color(0xFF160F2D),
                    borderRadius: BorderRadius.circular(14),
                    border: Border.all(
                      color: selectedAspectRatio == "16:9"
                          ? const Color(0xFFFF1744)
                          : Colors.white24,
                      width: selectedAspectRatio == "16:9" ? 2 : 1,
                    ),
                  ),
                  child: Column(
                    children: [
                      Container(
                        width: 40,
                        height: 40,
                        decoration: BoxDecoration(
                          color: const Color(0xFFFF0000),
                          borderRadius: BorderRadius.circular(10),
                        ),
                        child: const Icon(
                          Icons.play_arrow_rounded,
                          color: Colors.white,
                          size: 28,
                        ),
                      ),
                      const SizedBox(height: 8),
                      const Text(
                        "YouTube / Mini Series",
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Colors.white,
                          fontWeight: FontWeight.bold,
                          fontSize: 12,
                        ),
                      ),
                      const SizedBox(height: 2),
                      const Text(
                        "16:9 Landscape · 1280×720",
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Color(0xFF00E5FF),
                          fontSize: 10,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 14),

        // 1B. Moj / Pocket FM Part-Wise Micro-Drama Series Mode (Default) vs Single Movie Mode
        Row(
          children: [
            Expanded(
              child: ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: selectedSeriesMode == "series"
                      ? const Color(0xFFFF007F)
                      : const Color(0xFF1B1236),
                  padding: const EdgeInsets.symmetric(vertical: 11),
                ),
                onPressed: () {
                  selectedSeriesMode = "series";
                  _updateDataEstimate();
                },
                child: const Text(
                  "📱 Part-Wise Series (Default)",
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: Colors.white,
                    fontWeight: FontWeight.bold,
                    fontSize: 11,
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: selectedSeriesMode == "single"
                      ? const Color(0xFF00E5FF)
                      : const Color(0xFF1B1236),
                  padding: const EdgeInsets.symmetric(vertical: 11),
                ),
                onPressed: () {
                  selectedSeriesMode = "single";
                  _updateDataEstimate();
                },
                child: Text(
                  "🎬 Single Movie Mode",
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: selectedSeriesMode == "single"
                        ? Colors.black
                        : Colors.white,
                    fontWeight: FontWeight.bold,
                    fontSize: 11,
                  ),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        if (selectedSeriesMode == "series") ...[
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFF1B1236),
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: const Color(0xFFFF007F).withOpacity(0.45)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  "🔥 Pocket FM / Moj Micro-Drama Series Settings:",
                  style: TextStyle(
                    color: Color(0xFFFFD600),
                    fontWeight: FontWeight.bold,
                    fontSize: 12,
                  ),
                ),
                const SizedBox(height: 8),
                Row(
                  children: [1, 2].map((mins) {
                    final active = selectedPartDuration == mins;
                    return Expanded(
                      child: Padding(
                        padding: EdgeInsets.only(right: mins == 1 ? 8 : 0),
                        child: OutlinedButton(
                          style: OutlinedButton.styleFrom(
                            backgroundColor: active
                                ? const Color(0xFFFF007F).withOpacity(0.25)
                                : const Color(0xFF120B24),
                            side: BorderSide(
                              color: active
                                  ? const Color(0xFFFF007F)
                                  : Colors.white24,
                            ),
                          ),
                          onPressed: () {
                            selectedPartDuration = mins;
                            _updateDataEstimate();
                          },
                          child: Text(
                            "$mins Min / Part",
                            style: TextStyle(
                              color: active ? Colors.white : Colors.white70,
                              fontWeight: FontWeight.bold,
                              fontSize: 12,
                            ),
                          ),
                        ),
                      ),
                    );
                  }).toList(),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 6,
                  children: [3, 5, 10, 15].map((cnt) {
                    final active = selectedTotalParts == cnt;
                    return ChoiceChip(
                      label: Text(
                        "$cnt Parts",
                        style: TextStyle(
                          color: active ? Colors.black : Colors.white,
                          fontWeight: FontWeight.bold,
                          fontSize: 12,
                        ),
                      ),
                      selected: active,
                      selectedColor: const Color(0xFF00E5FF),
                      backgroundColor: const Color(0xFF281854),
                      onSelected: (_) {
                        selectedTotalParts = cnt;
                        _updateDataEstimate();
                      },
                    );
                  }).toList(),
                ),
                const SizedBox(height: 6),
                const Text(
                  "⚡ Part 1 सबसे पहले (~35s में) Play के लिए तैयार होगा · हर Part के अंत में Cliffhanger सस्पेंस और Auto-Next!",
                  style: TextStyle(color: Colors.white70, fontSize: 11),
                ),
              ],
            ),
          ),
        ] else ...[
          DropdownButton<int>(
            value: [1, 2, 5, 10, 30].contains(selectedMinutes)
                ? selectedMinutes
                : 1,
            isExpanded: true,
            dropdownColor: const Color(0xFF1B1236),
            items: const [
              DropdownMenuItem(
                value: 1,
                child: Text("1 मिनट (Reel / Short - ~15 MB)"),
              ),
              DropdownMenuItem(
                value: 2,
                child: Text("2 मिनट (Mini Clip - ~30 MB)"),
              ),
              DropdownMenuItem(
                value: 5,
                child: Text("5 मिनट (Short Film - ~80 MB)"),
              ),
              DropdownMenuItem(
                value: 10,
                child: Text("10 मिनट (Episode - ~160 MB)"),
              ),
              DropdownMenuItem(
                value: 30,
                child: Text("30 मिनट (Full Movie - ~480 MB)"),
              ),
            ],
            onChanged: (val) {
              selectedMinutes = val!;
              _updateDataEstimate();
            },
          ),
        ],
        const SizedBox(height: 8),
        Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: const Color(0xFF00E5FF).withOpacity(0.15),
            borderRadius: BorderRadius.circular(10),
          ),
          child: Text(
            dataEstimateText,
            style: const TextStyle(fontSize: 12, color: Color(0xFF00E5FF)),
          ),
        ),
        const SizedBox(height: 16),

        // 2. Character Photos + Guided 5-Angle 3D Face Scan
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            const Text(
              "2. कैरेक्टर 3D फेस स्कैन:",
              style: TextStyle(
                fontWeight: FontWeight.bold,
                color: Color(0xFF00E5FF),
              ),
            ),
            ElevatedButton.icon(
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFFFF007F),
              ),
              onPressed: _addCharacter,
              icon: const Icon(Icons.face_retouching_natural, color: Colors.white),
              label: const Text(
                "🎭 3D Face Scan / जोड़ें",
                style: TextStyle(color: Colors.white, fontSize: 12),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        if (characters.isEmpty)
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFF1B1236),
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: Colors.white12),
            ),
            child: const Text(
              "🎭 ऊपर '3D Face Scan / जोड़ें' दबाकर 5 एंगल (Front, Left, Right, Up, Down) स्कैन करें या गैलरी से फोटो चुनें।",
              style: TextStyle(color: Colors.white60, fontSize: 11),
            ),
          )
        else
          Column(
            children: characters.map((c) {
              final Map<String, File> angMap = c['angles'] is Map
                  ? Map<String, File>.from(c['angles'] as Map)
                  : {};
              final int angCount = angMap.length;
              final bool is5Ready =
                  (c['is5AngleReady'] == true) || angCount >= 5;
              return Container(
                margin: const EdgeInsets.only(bottom: 8),
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: const Color(0xFF211447),
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(
                    color: is5Ready
                        ? Colors.greenAccent.withOpacity(0.6)
                        : Colors.white12,
                  ),
                ),
                child: Row(
                  children: [
                    ClipOval(
                      child: SizedBox(
                        width: 44,
                        height: 44,
                        child: Image.file(
                          c['file'] as File,
                          fit: BoxFit.cover,
                          alignment: Alignment.topCenter,
                        ),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            "${c['name']} (${c['pitch'] ?? 'normal'})",
                            style: const TextStyle(
                              color: Colors.white,
                              fontWeight: FontWeight.bold,
                              fontSize: 13,
                            ),
                          ),
                          const SizedBox(height: 3),
                          if (is5Ready)
                            const Text(
                              "✅ 5-Angle 3D Face Profile Ready!",
                              style: TextStyle(
                                color: Colors.greenAccent,
                                fontWeight: FontWeight.bold,
                                fontSize: 11,
                              ),
                            )
                          else
                            Text(
                              "${angCount > 0 ? angCount : 1}/5 Angles Locked",
                              style: const TextStyle(
                                color: Color(0xFF00E5FF),
                                fontSize: 11,
                              ),
                            ),
                        ],
                      ),
                    ),
                    IconButton(
                      icon: const Icon(
                        Icons.delete_outline,
                        color: Colors.redAccent,
                        size: 20,
                      ),
                      onPressed: () {
                        setState(() => characters.remove(c));
                        _updateDataEstimate();
                      },
                    ),
                  ],
                ),
              );
            }).toList(),
          ),
        const SizedBox(height: 16),

        // 3. 2-Tab Story Writer Mode (AI vs Manual Custom Script)
        const Text(
          "3. कहानी और स्क्रिप्ट मोड चुनें:",
          style: TextStyle(
            fontWeight: FontWeight.bold,
            color: Color(0xFF00E5FF),
          ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: storyMode == "ai"
                      ? const Color(0xFFFF007F)
                      : const Color(0xFF1B1236),
                ),
                onPressed: () => setState(() => storyMode = "ai"),
                child: const Text(
                  "✨ AI से लिखवाओ",
                  style: TextStyle(color: Colors.white, fontSize: 12),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: storyMode == "manual"
                      ? const Color(0xFF00E5FF)
                      : const Color(0xFF1B1236),
                ),
                onPressed: () => setState(() => storyMode = "manual"),
                child: Text(
                  "✍️ खुद स्क्रिप्ट लिखो",
                  style: TextStyle(
                    color: storyMode == "manual" ? Colors.black : Colors.white,
                    fontWeight: FontWeight.bold,
                    fontSize: 12,
                  ),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),

        if (storyMode == "ai") ...[
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFF1B1236),
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: const Color(0xFFFF007F).withOpacity(0.5)),
            ),
            child: Column(
              children: [
                DropdownButtonFormField<String>(
                  value: selectedGenre,
                  dropdownColor: const Color(0xFF1B1236),
                  decoration: const InputDecoration(
                    labelText: "Genre चुनें",
                    border: OutlineInputBorder(),
                    isDense: true,
                  ),
                  items: _genres
                      .map((g) => DropdownMenuItem(value: g, child: Text(g)))
                      .toList(),
                  onChanged: (v) => setState(() => selectedGenre = v!),
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: aiTopicController,
                  decoration: const InputDecoration(
                    border: OutlineInputBorder(),
                    isDense: true,
                    labelText: "1 लाइन का टॉपिक लिखें",
                    hintText: "जैसे: गाँव का लड़का जिसे जादुई शक्ति मिलती है",
                  ),
                ),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton.icon(
                    style: ElevatedButton.styleFrom(
                      backgroundColor: const Color(0xFFFFD600),
                    ),
                    onPressed: isGeneratingStory ? null : _generateStoryWithAI,
                    icon: const Icon(Icons.auto_awesome, color: Colors.black),
                    label: Text(
                      isGeneratingStory
                          ? "AI पूरी कहानी लिख रहा है..."
                          : "Generate Full Story with AI",
                      style: const TextStyle(
                        color: Colors.black,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 10),
        ],

        TextField(
          controller: ideaController,
          maxLines: 5,
          decoration: InputDecoration(
            filled: true,
            fillColor: const Color(0xFF1B1236),
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(14),
            ),
            labelText: storyMode == "ai"
                ? "AI की लिखी कहानी (आप एडिट भी कर सकते हैं)"
                : "खुद की पूरी कहानी / स्क्रिप्ट (Character: Dialogue)",
            hintText: storyMode == "ai"
                ? "ऊपर Generate दबाएँ या खुद लिखें..."
                : "[Location: Purani Haveli | Mood: Suspense]\nHero: यहाँ कोई है...",
          ),
        ),
        const SizedBox(height: 16),

        // 4. Cinematic Controls (Language, Music Mood, Location, Watermark, Custom BG)
        const Text(
          "4. भाषा, म्यूज़िक मूड, लोकेशन और चैनल वॉटरमार्क:",
          style: TextStyle(
            fontWeight: FontWeight.bold,
            color: Color(0xFF00E5FF),
          ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: DropdownButtonFormField<String>(
                value: selectedLanguage,
                dropdownColor: const Color(0xFF1B1236),
                decoration: const InputDecoration(
                  labelText: "🗣️ भाषा (Language)",
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
                items: _languages
                    .map((l) => DropdownMenuItem(value: l, child: Text(l)))
                    .toList(),
                onChanged: (v) => setState(() => selectedLanguage = v!),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: DropdownButtonFormField<String>(
                value: defaultMood,
                dropdownColor: const Color(0xFF1B1236),
                decoration: const InputDecoration(
                  labelText: "🎵 Music Mood",
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
                items: _moods
                    .map((m) => DropdownMenuItem(value: m, child: Text(m)))
                    .toList(),
                onChanged: (v) => setState(() => defaultMood = v!),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: TextField(
                controller: locationController,
                decoration: const InputDecoration(
                  labelText: "Location (Auto)",
                  hintText: "Gaon ka Khet",
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: TextField(
                controller: watermarkController,
                decoration: const InputDecoration(
                  labelText: "🏷️ Channel Watermark",
                  hintText: "My YouTube Studio",
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        DropdownButtonFormField<String>(
          value: watermarkPosition,
          dropdownColor: const Color(0xFF1B1236),
          decoration: const InputDecoration(
            labelText: "Watermark Position",
            border: OutlineInputBorder(),
            isDense: true,
          ),
          items: const [
            DropdownMenuItem(value: "top-right", child: Text("Top Right")),
            DropdownMenuItem(value: "bottom-right", child: Text("Bottom Right")),
            DropdownMenuItem(value: "top-left", child: Text("Top Left")),
          ],
          onChanged: (v) => setState(() => watermarkPosition = v!),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: OutlinedButton.icon(
                onPressed: _pickCustomBackground,
                icon: const Icon(Icons.wallpaper, color: Color(0xFFFFD600)),
                label: Text(
                  customBgFile != null
                      ? "Custom BG Photo Selected ✓"
                      : "Custom Background Image (Optional)",
                  style: const TextStyle(color: Colors.white, fontSize: 12),
                ),
              ),
            ),
            if (customBgFile != null)
              IconButton(
                icon: const Icon(Icons.close, color: Colors.redAccent),
                onPressed: () => setState(() => customBgFile = null),
              ),
          ],
        ),

        const SizedBox(height: 20),
        ElevatedButton(
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFFFF007F),
            padding: const EdgeInsets.symmetric(vertical: 16),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(16),
            ),
          ),
          onPressed: isSubmitting ? null : _startAutonomousMovie,
          child: Text(
            isSubmitting
                ? "अपलोड हो रहा है..."
                : (isAdmin
                    ? "🚀 OK - मूवी बनाना शुरू करें (Admin Free)"
                    : userCredits > 0
                        ? "🎬 OK - मूवी बनाएं (1 पास)"
                        : walletBalance >= moviePrice
                            ? "🎬 OK - मूवी बनाएं (₹$moviePrice Wallet)"
                            : "🎟️ मूवी पास खरीदें (₹$moviePrice)"),
            style: const TextStyle(
              fontSize: 15,
              fontWeight: FontWeight.bold,
              color: Colors.white,
            ),
          ),
        ),
        if (statusMessage.isNotEmpty) ...[
          const SizedBox(height: 12),
          Text(
            statusMessage,
            style: const TextStyle(color: Color(0xFFFFD600)),
          ),
        ]
      ],
    );
  }

  Widget _buildStudioScreen() {
    return ListView(
      children: [
        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: isCompleted
                ? const Color(0xFF103324)
                : const Color(0xFF1B1236),
            borderRadius: BorderRadius.circular(16),
            border: Border.all(
              color: isCompleted
                  ? Colors.greenAccent
                  : const Color(0xFFFF007F).withOpacity(0.6),
            ),
          ),
          child: Column(
            children: [
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text(
                    "प्रोजेक्ट ID: $activeProjectId",
                    style: const TextStyle(fontSize: 12, color: Colors.white70),
                  ),
                  Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                    decoration: BoxDecoration(
                      color: isCompleted
                          ? Colors.green
                          : const Color(0xFFFF007F),
                      borderRadius: BorderRadius.circular(20),
                    ),
                    child: Text(
                      isCompleted ? "✅ 100% READY" : "⚡ $progress%",
                      style: const TextStyle(
                        color: Colors.white,
                        fontWeight: FontWeight.w900,
                        fontSize: 12,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: LinearProgressIndicator(
                  value: (progress.clamp(2, 100)) / 100.0,
                  minHeight: 12,
                  color: isCompleted
                      ? Colors.greenAccent
                      : const Color(0xFFFF007F),
                  backgroundColor: const Color(0xFF281854),
                ),
              ),
              const SizedBox(height: 8),
              Text(
                statusMessage.isNotEmpty
                    ? statusMessage
                    : "लाइव रेंडरिंग चालू है (हर 3 सेकंड में ऑटो-अपडेट)...",
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 14,
                  fontWeight: FontWeight.bold,
                  color: Color(0xFFFFD600),
                ),
              ),
              if (!isCompleted && readyParts.contains(1)) ...[
                const SizedBox(height: 12),
                Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Colors.green.withOpacity(0.18),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: Colors.greenAccent),
                  ),
                  child: Row(
                    children: [
                      const Expanded(
                        child: Text(
                          "🔥 Part 1 तैयार है — अभी Play करें! बाकी Parts बैकग्राउंड में बन रहे हैं...",
                          style: TextStyle(
                            color: Colors.greenAccent,
                            fontWeight: FontWeight.bold,
                            fontSize: 12,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      ElevatedButton.icon(
                        style: ElevatedButton.styleFrom(
                          backgroundColor: const Color(0xFF00E5FF),
                          padding: const EdgeInsets.symmetric(
                            horizontal: 12,
                            vertical: 8,
                          ),
                        ),
                        onPressed: () => _playOnlinePreview("PART_1"),
                        icon: const Icon(
                          Icons.play_arrow,
                          color: Colors.black,
                          size: 18,
                        ),
                        label: const Text(
                          "Play Part 1",
                          style: TextStyle(
                            color: Colors.black,
                            fontWeight: FontWeight.bold,
                            fontSize: 12,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
              if (isCompleted) ...[
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: ElevatedButton.icon(
                        style: ElevatedButton.styleFrom(
                          backgroundColor: const Color(0xFF00E5FF),
                          padding: const EdgeInsets.symmetric(vertical: 12),
                        ),
                        onPressed: () => _playOnlinePreview(
                          activeSeriesMode == "series" && readyParts.contains(1)
                              ? "PART_1"
                              : "FULL_MOVIE",
                        ),
                        icon: const Icon(Icons.play_arrow, color: Colors.black),
                        label: const Text(
                          "Ready! Play",
                          style: TextStyle(
                            color: Colors.black,
                            fontWeight: FontWeight.w900,
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: ElevatedButton.icon(
                        style: ElevatedButton.styleFrom(
                          backgroundColor: Colors.green,
                          padding: const EdgeInsets.symmetric(vertical: 12),
                        ),
                        onPressed: () => _downloadVideo("FULL_MOVIE"),
                        icon: const Icon(Icons.download, color: Colors.white),
                        label: const Text(
                          "Download HD",
                          style: TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w900,
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ],
            ],
          ),
        ),
        const SizedBox(height: 12),
        // Moj / Pocket FM Top Part Tabs ([Part 1] [Part 2] [Part 3]...) with Auto-Next
        if (readyParts.isNotEmpty) ...[
          SizedBox(
            height: 40,
            child: ListView(
              scrollDirection: Axis.horizontal,
              children: [
                ...readyParts.map((p) {
                  final key = "PART_$p";
                  final isCurrent = currentlyPlayingType == key;
                  return Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(
                        "Part $p",
                        style: TextStyle(
                          color: isCurrent ? Colors.white : Colors.white70,
                          fontWeight: FontWeight.bold,
                          fontSize: 12,
                        ),
                      ),
                      selected: isCurrent,
                      selectedColor: const Color(0xFFFF007F),
                      backgroundColor: const Color(0xFF1B1236),
                      onSelected: (_) => _playOnlinePreview(key),
                    ),
                  );
                }),
                if (isCompleted)
                  ChoiceChip(
                    label: Text(
                      "🎬 Full",
                      style: TextStyle(
                        color: currentlyPlayingType == "FULL_MOVIE"
                            ? Colors.black
                            : Colors.white70,
                        fontWeight: FontWeight.bold,
                        fontSize: 12,
                      ),
                    ),
                    selected: currentlyPlayingType == "FULL_MOVIE",
                    selectedColor: const Color(0xFFFFD600),
                    backgroundColor: const Color(0xFF1B1236),
                    onSelected: (_) => _playOnlinePreview("FULL_MOVIE"),
                  ),
              ],
            ),
          ),
          const SizedBox(height: 10),
        ],
        if (_chewieController != null &&
            _videoController!.value.isInitialized) ...[
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                "प्रिव्यू चल रहा है: $currentlyPlayingType (${activeAspectRatio == "9:16" ? "9:16 Reels" : "16:9 HD"})",
                style: const TextStyle(
                  fontWeight: FontWeight.bold,
                  color: Color(0xFF00E5FF),
                  fontSize: 12,
                ),
              ),
              if (activeSeriesMode == "series")
                const Text(
                  "Auto-Next ON",
                  style: TextStyle(
                    color: Colors.greenAccent,
                    fontSize: 11,
                    fontWeight: FontWeight.bold,
                  ),
                ),
            ],
          ),
          const SizedBox(height: 8),
          Center(
            child: ConstrainedBox(
              constraints: BoxConstraints(
                maxHeight: activeAspectRatio == "9:16" ? 500 : 280,
              ),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(14),
                child: AspectRatio(
                  aspectRatio: _videoController!.value.aspectRatio > 0
                      ? _videoController!.value.aspectRatio
                      : (activeAspectRatio == "9:16" ? 9 / 16 : 16 / 9),
                  child: Chewie(controller: _chewieController!),
                ),
              ),
            ),
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: ElevatedButton.icon(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.orange,
                  ),
                  onPressed: _openEditDialogueDialog,
                  icon: const Icon(Icons.edit, color: Colors.white, size: 18),
                  label: const Text(
                    "डायलॉग बदलें",
                    style: TextStyle(color: Colors.white, fontSize: 12),
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: ElevatedButton.icon(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF00E5FF),
                  ),
                  onPressed: _openCustomizeSceneDialog,
                  icon: const Icon(Icons.tune, color: Colors.black, size: 18),
                  label: const Text(
                    "लोकेशन/BG",
                    style: TextStyle(
                      color: Colors.black,
                      fontWeight: FontWeight.bold,
                      fontSize: 12,
                    ),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            child: ElevatedButton.icon(
              style: ElevatedButton.styleFrom(backgroundColor: Colors.green),
              onPressed: () => _downloadVideo(currentlyPlayingType!),
              icon: const Icon(Icons.download, color: Colors.white),
              label: const Text(
                "📥 Phone Gallery (Movies/CinePopAI) में सेव करें",
                style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
              ),
            ),
          ),
          const Divider(height: 24),
        ],
        const Text(
          "तैयार पार्ट्स (पहले Play करके चेक करें, फिर Gallery में Download करें):",
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 8),
        ...readyParts.map(
          (p) => Card(
            color: const Color(0xFF1B1236),
            child: ListTile(
              title: Text(
                activeSeriesMode == "series"
                    ? "📱 Part $p (Micro-Drama · Auto-Next)"
                    : "पार्ट $p",
              ),
              trailing: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  IconButton(
                    icon: const Icon(
                      Icons.play_circle_fill,
                      color: Color(0xFF00E5FF),
                      size: 32,
                    ),
                    onPressed: () => _playOnlinePreview("PART_$p"),
                  ),
                  IconButton(
                    icon: const Icon(
                      Icons.download_for_offline,
                      color: Colors.greenAccent,
                      size: 32,
                    ),
                    onPressed: () => _downloadVideo("PART_$p"),
                  ),
                ],
              ),
            ),
          ),
        ),
        if (isCompleted) ...[
          const SizedBox(height: 12),
          Card(
            color: const Color(0xFF2E145E),
            child: ListTile(
              title: const Text(
                "🎬 पूरी फुल मूवी (Full Movie)",
                style: TextStyle(fontWeight: FontWeight.bold),
              ),
              trailing: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  IconButton(
                    icon: const Icon(
                      Icons.play_circle_fill,
                      color: Color(0xFFFFD600),
                      size: 34,
                    ),
                    onPressed: () => _playOnlinePreview("FULL_MOVIE"),
                  ),
                  IconButton(
                    icon: const Icon(
                      Icons.download_for_offline,
                      color: Colors.greenAccent,
                      size: 34,
                    ),
                    onPressed: () => _downloadVideo("FULL_MOVIE"),
                  ),
                ],
              ),
            ),
          ),
        ],
        const SizedBox(height: 16),
        // Pocket Bollywood Studio Pro Tools: Trailer/Reel, AI Poster, Multi-Language Dubbing
        const Text(
          "✨ Pocket Bollywood Pro Tools:",
          style: TextStyle(
            fontWeight: FontWeight.bold,
            color: Color(0xFFFFD600),
          ),
        ),
        const SizedBox(height: 8),
        ElevatedButton.icon(
          style: ElevatedButton.styleFrom(
            backgroundColor: isCompleted
                ? const Color(0xFFFF007F)
                : const Color(0xFF2B2340),
            padding: const EdgeInsets.symmetric(vertical: 13),
          ),
          onPressed: (!isCompleted || generatingTrailer)
              ? null
              : _generateTrailerAndReel,
          icon: Icon(
            isCompleted ? Icons.movie_filter : Icons.lock,
            color: isCompleted ? Colors.white : Colors.white54,
          ),
          label: Text(
            !isCompleted
                ? "🔒 Movie poori hone ke baad Trailer khulega"
                : generatingTrailer
                    ? "60-Sec Trailer & 9:16 Reel बन रहे हैं..."
                    : "🎬 Generate 60-Sec Trailer / 9:16 Reel",
            style: TextStyle(
              color: isCompleted ? Colors.white : Colors.white60,
              fontWeight: FontWeight.bold,
            ),
          ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: ElevatedButton.icon(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF7000FF),
                ),
                onPressed: (generatingPoster || activeProjectId == null)
                    ? null
                    : () => _generateAndShowPoster(activeProjectId!),
                icon: const Icon(Icons.photo_album, color: Colors.white, size: 18),
                label: Text(
                  generatingPoster ? "Creating..." : "🖼️ AI Poster",
                  style: const TextStyle(color: Colors.white, fontSize: 12),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: ElevatedButton.icon(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF00E5FF),
                ),
                onPressed: dubbingMovie ? null : _openDubLanguageDialog,
                icon: const Icon(Icons.translate, color: Colors.black, size: 18),
                label: Text(
                  dubbingMovie ? "Dubbing..." : "🗣️ Dub Language",
                  style: const TextStyle(
                    color: Colors.black,
                    fontWeight: FontWeight.bold,
                    fontSize: 12,
                  ),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 24),
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceEvenly,
          children: [
            TextButton.icon(
              onPressed: () async {
                final prefs = await SharedPreferences.getInstance();
                await prefs.remove('active_project_id');
                setState(() {
                  activeProjectId = null;
                  readyParts = [];
                  progress = 0;
                  isCompleted = false;
                  currentlyPlayingType = null;
                });
              },
              icon: const Icon(Icons.add_circle_outline, color: Color(0xFF00E5FF)),
              label: const Text(
                "नया प्रोजेक्ट शुरू करें",
                style: TextStyle(color: Color(0xFF00E5FF)),
              ),
            ),
            if (activeProjectId != null)
              TextButton.icon(
                onPressed: () => _deleteLibraryMovie(
                  activeProjectId!,
                  "यह वीडियो ($activeProjectId)",
                ),
                icon: const Icon(Icons.delete_outline, color: Colors.redAccent),
                label: const Text(
                  "डिलीट करें",
                  style: TextStyle(color: Colors.redAccent),
                ),
              ),
          ],
        )
      ],
    );
  }

  // ==================== TAB 1: CUSTOM VIDEO AI FACE-SWAP STUDIO ====================

  Widget _buildFaceSwapScreen() {
    return ListView(
      children: [
        const Text(
          "🎭 Custom Video AI Face-Swap Studio",
          style: TextStyle(
            fontSize: 20,
            fontWeight: FontWeight.w900,
            color: Color(0xFFFFD600),
          ),
        ),
        const SizedBox(height: 4),
        const Text(
          "अपना कोई भी मूवी सीन, फाइट क्लिप या रील अपलोड करें और उसमें अपना चेहरा + अपनी हिंदी/रीजनल डबिंग लगाएँ!",
          style: TextStyle(fontSize: 12, color: Colors.white70),
        ),
        const SizedBox(height: 14),
        TextField(
          controller: fsTitleController,
          decoration: const InputDecoration(
            filled: true,
            fillColor: Color(0xFF1B1236),
            border: OutlineInputBorder(),
            labelText: "वीडियो का नाम (Title)",
          ),
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: ElevatedButton.icon(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF7000FF),
                  padding: const EdgeInsets.symmetric(vertical: 14),
                ),
                onPressed: _pickFaceSwapVideo,
                icon: const Icon(Icons.video_file, color: Colors.white),
                label: Text(
                  fsVideoFile != null ? "Video Selected ✓" : "1. Video चुनें",
                  style: const TextStyle(color: Colors.white, fontSize: 12),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: ElevatedButton.icon(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFFF007F),
                  padding: const EdgeInsets.symmetric(vertical: 14),
                ),
                onPressed: _pickFaceSwapFace,
                icon: const Icon(Icons.face, color: Colors.white),
                label: Text(
                  fsFaceFile != null ? "Face Photo ✓" : "2. Face Photo",
                  style: const TextStyle(color: Colors.white, fontSize: 12),
                ),
              ),
            ),
          ],
        ),
        if (fsFaceFile != null) ...[
          const SizedBox(height: 10),
          Row(
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Image.file(
                  fsFaceFile!,
                  width: 68,
                  height: 68,
                  fit: BoxFit.cover,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  fsVideoFile != null
                      ? "✅ वीडियो और फेस फोटो तैयार हैं! नीचे Start दबाएँ।"
                      : "अब कृपया एक वीडियो क्लिप चुनें।",
                  style: const TextStyle(
                    fontSize: 12,
                    color: Colors.greenAccent,
                  ),
                ),
              ),
            ],
          ),
        ],
        const SizedBox(height: 12),
        TextField(
          controller: fsDubController,
          maxLines: 3,
          decoration: const InputDecoration(
            filled: true,
            fillColor: Color(0xFF1B1236),
            border: OutlineInputBorder(),
            labelText: "🗣️ कस्टम डायलॉग / वॉइसओवर डबिंग (Optional)",
            hintText: "खाली छोड़ेंगे तो वीडियो की असली आवाज़ ही रहेगी...",
          ),
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: DropdownButtonFormField<String>(
                value: fsDubLanguage,
                dropdownColor: const Color(0xFF1B1236),
                decoration: const InputDecoration(
                  labelText: "Dub Language",
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
                items: _languages
                    .map((l) => DropdownMenuItem(value: l, child: Text(l)))
                    .toList(),
                onChanged: (v) => setState(() => fsDubLanguage = v!),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: DropdownButtonFormField<String>(
                value: fsDubPitch,
                dropdownColor: const Color(0xFF1B1236),
                decoration: const InputDecoration(
                  labelText: "Voice Pitch",
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
                items: _pitchPresets
                    .map(
                      (p) => DropdownMenuItem(
                        value: p['key'],
                        child: Text(p['label']!),
                      ),
                    )
                    .toList(),
                onChanged: (v) => setState(() => fsDubPitch = v!),
              ),
            ),
          ],
        ),
        const SizedBox(height: 16),
        ElevatedButton.icon(
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFFFFD600),
            padding: const EdgeInsets.symmetric(vertical: 15),
          ),
          onPressed: fsSubmitting ? null : _startFaceSwapJob,
          icon: const Icon(Icons.auto_fix_high, color: Colors.black),
          label: Text(
            fsSubmitting
                ? "AI Face-Swap अपलोड हो रहा है..."
                : "🚀 Start AI Face-Swap Render",
            style: const TextStyle(
              color: Colors.black,
              fontWeight: FontWeight.bold,
              fontSize: 15,
            ),
          ),
        ),
        if (fsStatusText.isNotEmpty) ...[
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFF1B1236),
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: const Color(0xFF00E5FF)),
            ),
            child: Row(
              children: [
                if (fsStatus == "processing")
                  const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                if (fsStatus == "processing") const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    fsStatusText,
                    style: const TextStyle(color: Color(0xFF00E5FF), fontSize: 12),
                  ),
                ),
              ],
            ),
          ),
        ],
        if (_fsChewieController != null &&
            _fsVideoController != null &&
            _fsVideoController!.value.isInitialized) ...[
          const SizedBox(height: 16),
          AspectRatio(
            aspectRatio: _fsVideoController!.value.aspectRatio,
            child: Chewie(controller: _fsChewieController!),
          ),
          const SizedBox(height: 10),
          if (fsActiveJobId != null)
            ElevatedButton.icon(
              style: ElevatedButton.styleFrom(backgroundColor: Colors.green),
              onPressed: () => _downloadDirectUrl(
                '$serverUrl/face-swap/download/$fsActiveJobId',
                'CinePop_FaceSwap_$fsActiveJobId.mp4',
              ),
              icon: const Icon(Icons.download, color: Colors.white),
              label: const Text(
                "⬇ Face-Swap HD MP4 डाउनलोड करें",
                style: TextStyle(color: Colors.white),
              ),
            ),
        ],
      ],
    );
  }

  // ==================== TAB 2: MY MOVIES CLOUD LIBRARY ====================

  Future<void> _deleteLibraryMovie(String projectId, String title) async {
    final bool? confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: const Text(
          "🗑️ वीडियो डिलीट करें?",
          style: TextStyle(color: Colors.redAccent, fontWeight: FontWeight.bold),
        ),
        content: Text(
          "क्या आप \"$title\" को हमेशा के लिए डिलीट करना चाहते हैं?",
          style: const TextStyle(color: Colors.white70, fontSize: 13),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text("नहीं", style: TextStyle(color: Colors.white70)),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.redAccent),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text(
              "हाँ, डिलीट करें",
              style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
            ),
          ),
        ],
      ),
    );
    if (confirmed != true) return;

    try {
      final emailParam = Uri.encodeComponent(userEmail ?? "");
      await http
          .delete(
            Uri.parse('$serverUrl/project/$projectId?user_email=$emailParam'),
          )
          .timeout(const Duration(seconds: 12));
      if (activeProjectId == projectId) {
        syncTimer?.cancel();
        _videoController?.dispose();
        _chewieController?.dispose();
        _videoController = null;
        _chewieController = null;
        final prefs = await SharedPreferences.getInstance();
        await prefs.remove('active_project_id');
      }
      if (mounted) {
        setState(() {
          libraryMovies.removeWhere(
            (m) => m['project_id'].toString() == projectId,
          );
          if (activeProjectId == projectId) {
            activeProjectId = null;
            readyParts = [];
            progress = 0;
            isCompleted = false;
            currentlyPlayingType = null;
          }
        });
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            backgroundColor: Colors.redAccent,
            content: Text("🗑️ मूवी सफलतापूर्वक डिलीट हो गई!"),
          ),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("डिलीट करने में समस्या: $e")),
        );
      }
    }
  }

  Future<void> _deleteLibraryFaceSwap(String jobId, String title) async {
    final bool? confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: const Text(
          "🗑️ Face-Swap वीडियो डिलीट करें?",
          style: TextStyle(color: Colors.redAccent, fontWeight: FontWeight.bold),
        ),
        content: Text(
          "क्या आप \"$title\" को हमेशा के लिए डिलीट करना चाहते हैं?",
          style: const TextStyle(color: Colors.white70, fontSize: 13),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text("नहीं", style: TextStyle(color: Colors.white70)),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.redAccent),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text(
              "हाँ, डिलीट करें",
              style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
            ),
          ),
        ],
      ),
    );
    if (confirmed != true) return;

    try {
      final emailParam = Uri.encodeComponent(userEmail ?? "");
      await http
          .delete(
            Uri.parse('$serverUrl/face-swap/$jobId?user_email=$emailParam'),
          )
          .timeout(const Duration(seconds: 12));
      if (mounted) {
        setState(() {
          libraryFaceSwaps.removeWhere(
            (j) => j['job_id'].toString() == jobId,
          );
        });
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            backgroundColor: Colors.redAccent,
            content: Text("🗑️ Face-Swap वीडियो डिलीट हो गया!"),
          ),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("डिलीट करने में समस्या: $e")),
        );
      }
    }
  }

  Widget _buildLibraryScreen() {
    return RefreshIndicator(
      onRefresh: _fetchCloudLibrary,
      child: ListView(
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const Text(
                "📚 My Movies Cloud Library",
                style: TextStyle(
                  fontSize: 19,
                  fontWeight: FontWeight.w900,
                  color: Color(0xFFFFD600),
                ),
              ),
              IconButton(
                icon: const Icon(Icons.refresh, color: Color(0xFF00E5FF)),
                onPressed: _fetchCloudLibrary,
              ),
            ],
          ),
          if (loadingLibrary)
            const Padding(
              padding: EdgeInsets.all(24),
              child: Center(child: CircularProgressIndicator()),
            ),
          const SizedBox(height: 8),
          const Text(
            "🎬 मेरी बनाई हुई मूवीज़:",
            style: TextStyle(
              fontWeight: FontWeight.bold,
              color: Color(0xFF00E5FF),
            ),
          ),
          const SizedBox(height: 6),
          if (!loadingLibrary && libraryMovies.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 12),
              child: Text(
                "अभी तक कोई मूवी नहीं बनी है। Studio टैब से पहली मूवी बनाएँ!",
                style: TextStyle(color: Colors.white60, fontSize: 12),
              ),
            ),
          ...libraryMovies.map(
            (m) => Card(
              color: const Color(0xFF1B1236),
              child: ListTile(
                title: Text(
                  (m['title'] ?? 'Movie').toString(),
                  style: const TextStyle(fontWeight: FontWeight.bold),
                ),
                subtitle: Text(
                  "${m['genre'] ?? 'Action'} • ${m['language'] ?? 'Hindi'} • ${m['progress'] ?? 0}%",
                  style: const TextStyle(fontSize: 12, color: Colors.white70),
                ),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    IconButton(
                      visualDensity: VisualDensity.compact,
                      icon: const Icon(Icons.photo_album, color: Color(0xFFFFD600)),
                      tooltip: "Movie Poster",
                      onPressed: () =>
                          _generateAndShowPoster(m['project_id'].toString()),
                    ),
                    IconButton(
                      visualDensity: VisualDensity.compact,
                      icon: const Icon(Icons.play_arrow, color: Color(0xFF00E5FF)),
                      tooltip: "Open in Studio",
                      onPressed: () {
                        setState(() {
                          activeProjectId = m['project_id'].toString();
                          currentTab = 0;
                        });
                        _startAutoSync();
                      },
                    ),
                    IconButton(
                      visualDensity: VisualDensity.compact,
                      icon: const Icon(
                        Icons.delete_outline,
                        color: Colors.redAccent,
                      ),
                      tooltip: "Delete Video",
                      onPressed: () => _deleteLibraryMovie(
                        m['project_id'].toString(),
                        (m['title'] ?? 'Movie').toString(),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          const Divider(height: 28),
          const Text(
            "🎭 मेरे AI Face-Swap वीडियो:",
            style: TextStyle(
              fontWeight: FontWeight.bold,
              color: Color(0xFFFF007F),
            ),
          ),
          const SizedBox(height: 6),
          if (!loadingLibrary && libraryFaceSwaps.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 12),
              child: Text(
                "अभी तक कोई Face-Swap वीडियो नहीं बना है।",
                style: TextStyle(color: Colors.white60, fontSize: 12),
              ),
            ),
          ...libraryFaceSwaps.map(
            (j) => Card(
              color: const Color(0xFF231342),
              child: ListTile(
                title: Text((j['title'] ?? 'Face-Swap').toString()),
                subtitle: Text(
                  "Status: ${j['status']} • ${j['created_at'] ?? ''}",
                  style: TextStyle(
                    fontSize: 12,
                    color: j['status'] == 'failed'
                        ? Colors.redAccent
                        : Colors.white70,
                  ),
                ),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (j['status'] == 'ready') ...[
                      IconButton(
                        visualDensity: VisualDensity.compact,
                        icon: const Icon(
                          Icons.play_circle_fill,
                          color: Color(0xFF00E5FF),
                        ),
                        onPressed: () {
                          setState(() => currentTab = 1);
                          _playFaceSwapPreview(j['job_id'].toString());
                        },
                      ),
                      IconButton(
                        visualDensity: VisualDensity.compact,
                        icon: const Icon(
                          Icons.download,
                          color: Colors.greenAccent,
                        ),
                        onPressed: () => _downloadDirectUrl(
                          '$serverUrl/face-swap/download/${j['job_id']}',
                          'CinePop_FaceSwap_${j['job_id']}.mp4',
                        ),
                      ),
                    ] else
                      Icon(
                        j['status'] == 'failed'
                            ? Icons.error_outline
                            : Icons.hourglass_top,
                        color: j['status'] == 'failed'
                            ? Colors.redAccent
                            : Colors.amber,
                        size: 20,
                      ),
                    IconButton(
                      visualDensity: VisualDensity.compact,
                      icon: const Icon(
                        Icons.delete_outline,
                        color: Colors.redAccent,
                      ),
                      tooltip: "Delete Face-Swap",
                      onPressed: () => _deleteLibraryFaceSwap(
                        j['job_id'].toString(),
                        (j['title'] ?? 'Face-Swap').toString(),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ==================== TAB 3: REFER & EARN VIRAL GROWTH ====================

  Widget _buildReferEarnScreen() {
    final shareText =
        "🎬 CinePop AI पर अपनी फोटो से 3 घंटे की बॉलीवुड मूवी और AI Face-Swap वीडियो बनाओ! मेरा Referral Code *$myReferralCode* डालो और पाओ 1 FREE मूवी पास 🎁";
    return ListView(
      children: [
        Container(
          padding: const EdgeInsets.all(18),
          decoration: BoxDecoration(
            gradient: const LinearGradient(
              colors: [Color(0xFF3A1268), Color(0xFF1A0B36)],
            ),
            borderRadius: BorderRadius.circular(18),
            border: Border.all(color: const Color(0xFFFFD600)),
          ),
          child: Column(
            children: [
              const Text(
                "🎁 Refer & Earn — Free Movie Passes!",
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w900,
                  color: Color(0xFFFFD600),
                ),
              ),
              const SizedBox(height: 6),
              const Text(
                "अपने दोस्त को अपना कोड शेयर करें — दोस्त के जुड़ते ही आपको और आपके दोस्त दोनों को 1-1 FREE मूवी पास मिलेगा!",
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, color: Colors.white70),
              ),
              const SizedBox(height: 14),
              Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
                decoration: BoxDecoration(
                  color: Colors.black54,
                  borderRadius: BorderRadius.circular(14),
                  border: Border.all(color: const Color(0xFF00E5FF)),
                ),
                child: Text(
                  myReferralCode.isEmpty ? "Loading..." : myReferralCode,
                  style: const TextStyle(
                    fontSize: 24,
                    letterSpacing: 3,
                    fontWeight: FontWeight.w900,
                    color: Color(0xFF00E5FF),
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Expanded(
                    child: ElevatedButton.icon(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF7000FF),
                      ),
                      onPressed: () {
                        Clipboard.setData(ClipboardData(text: myReferralCode));
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(content: Text("✅ Referral Code कॉपी हो गया!")),
                        );
                      },
                      icon: const Icon(Icons.copy, color: Colors.white, size: 18),
                      label: const Text(
                        "Copy Code",
                        style: TextStyle(color: Colors.white, fontSize: 12),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: ElevatedButton.icon(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: Colors.green,
                      ),
                      onPressed: () {
                        Clipboard.setData(ClipboardData(text: shareText));
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(
                            content: Text("✅ WhatsApp शेयर मैसेज कॉपी हो गया!"),
                          ),
                        );
                      },
                      icon: const Icon(Icons.share, color: Colors.white, size: 18),
                      label: const Text(
                        "Copy Share Msg",
                        style: TextStyle(color: Colors.white, fontSize: 12),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
        const SizedBox(height: 14),
        Row(
          children: [
            Expanded(
              child: Container(
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(
                  color: const Color(0xFF1B1236),
                  borderRadius: BorderRadius.circular(14),
                ),
                child: Column(
                  children: [
                    Text(
                      "$referralsCount",
                      style: const TextStyle(
                        fontSize: 22,
                        fontWeight: FontWeight.bold,
                        color: Color(0xFF00E5FF),
                      ),
                    ),
                    const Text(
                      "कुल दोस्त जुड़े",
                      style: TextStyle(fontSize: 12, color: Colors.white70),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Container(
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(
                  color: const Color(0xFF1B1236),
                  borderRadius: BorderRadius.circular(14),
                ),
                child: Column(
                  children: [
                    Text(
                      "+$referralEarnings",
                      style: const TextStyle(
                        fontSize: 22,
                        fontWeight: FontWeight.bold,
                        color: Colors.greenAccent,
                      ),
                    ),
                    const Text(
                      "फ्री मूवी पास मिले",
                      style: TextStyle(fontSize: 12, color: Colors.white70),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 16),
        if (referredBy.isEmpty) ...[
          const Text(
            "दोस्त का Referral Code डालें (+1 Free Pass):",
            style: TextStyle(
              fontWeight: FontWeight.bold,
              color: Color(0xFFFFD600),
            ),
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: TextField(
                  controller: applyReferralController,
                  textCapitalization: TextCapitalization.characters,
                  decoration: const InputDecoration(
                    filled: true,
                    fillColor: Color(0xFF1B1236),
                    border: OutlineInputBorder(),
                    hintText: "जैसे: CPABC123",
                    isDense: true,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFFF007F),
                ),
                onPressed: _applyFriendReferralCode,
                child: const Text(
                  "Apply",
                  style: TextStyle(color: Colors.white),
                ),
              ),
            ],
          ),
        ] else
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFF123026),
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: Colors.greenAccent),
            ),
            child: Text(
              "✅ आपने Referral Bonus ($referredBy) क्लेम कर लिया है!",
              style: const TextStyle(color: Colors.greenAccent, fontSize: 12),
            ),
          ),
      ],
    );
  }
}

class AdminDashboardScreen extends StatefulWidget {
  final String serverUrl;
  final String adminEmail;
  final String adminPin;
  const AdminDashboardScreen({
    super.key,
    required this.serverUrl,
    required this.adminEmail,
    required this.adminPin,
  });

  @override
  State<AdminDashboardScreen> createState() => _AdminDashboardScreenState();
}

class _AdminDashboardScreenState extends State<AdminDashboardScreen> {
  int totalInstalls = 0;
  int totalRevenue = 0;
  int totalWallets = 0;
  int totalMovies = 0;
  int totalReferrals = 0;
  int totalFaceSwaps = 0;
  List<dynamic> users = [];
  List<dynamic> pendingPayments = [];
  List<dynamic> promoCodes = [];
  List<dynamic> referrals = [];
  bool loading = true;

  @override
  void initState() {
    super.initState();
    _fetchAdminData();
  }

  Future<void> _fetchAdminData() async {
    setState(() => loading = true);
    try {
      final res = await http.get(
        Uri.parse(
          '${widget.serverUrl}/admin/stats/?admin_email=${Uri.encodeComponent(widget.adminEmail)}&admin_pin=${Uri.encodeComponent(widget.adminPin)}',
        ),
      );
      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        setState(() {
          totalInstalls = data['total_installs'] ?? 0;
          totalRevenue = data['total_revenue'] ?? 0;
          totalWallets = data['total_wallet_balance'] ?? 0;
          totalMovies = data['total_movies_made'] ?? 0;
          totalReferrals = data['total_referrals'] ?? 0;
          totalFaceSwaps = data['total_face_swaps'] ?? 0;
          users = data['users'] ?? [];
          pendingPayments = data['pending_payments'] ?? [];
          promoCodes = data['promo_codes'] ?? [];
          referrals = data['referrals'] ?? [];
        });
      }
    } catch (_) {}
    if (mounted) setState(() => loading = false);
  }

  Future<void> _showAdjustWalletDialog(Map<String, dynamic> targetUser) async {
    final amountCtrl = TextEditingController(text: "50");
    final creditsCtrl = TextEditingController(text: "0");
    final noteCtrl = TextEditingController();

    await showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: Text(
          "💰 वॉलेट / पास अपडेट करें",
          style: const TextStyle(color: Color(0xFFFFD600), fontSize: 17),
        ),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                "${targetUser['name']} (${targetUser['email']})",
                style: const TextStyle(fontSize: 12, color: Colors.white70),
              ),
              Text(
                "मौजूदा वॉलेट: ₹${targetUser['wallet_balance'] ?? 0} | पास: ${targetUser['credits'] ?? 0}",
                style: const TextStyle(
                  fontSize: 12,
                  color: Colors.greenAccent,
                  fontWeight: FontWeight.bold,
                ),
              ),
              const SizedBox(height: 10),
              Wrap(
                spacing: 6,
                children: [
                  ActionChip(
                    label: const Text("+₹50"),
                    onPressed: () => amountCtrl.text = "50",
                  ),
                  ActionChip(
                    label: const Text("+₹100"),
                    onPressed: () => amountCtrl.text = "100",
                  ),
                  ActionChip(
                    label: const Text("+₹500"),
                    onPressed: () => amountCtrl.text = "500",
                  ),
                  ActionChip(
                    label: const Text("-₹50"),
                    onPressed: () => amountCtrl.text = "-50",
                  ),
                ],
              ),
              const SizedBox(height: 10),
              TextField(
                controller: amountCtrl,
                keyboardType:
                    const TextInputType.numberWithOptions(signed: true),
                decoration: const InputDecoration(
                  border: OutlineInputBorder(),
                  labelText: "Wallet ₹ Amount (+ या -)",
                ),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: creditsCtrl,
                keyboardType:
                    const TextInputType.numberWithOptions(signed: true),
                decoration: const InputDecoration(
                  border: OutlineInputBorder(),
                  labelText: "Movie Passes (+ या -)",
                ),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: noteCtrl,
                decoration: const InputDecoration(
                  border: OutlineInputBorder(),
                  labelText: "Note (Optional)",
                  hintText: "जैसे: UPI Received / Bonus",
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text("रद्द करें"),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.green),
            onPressed: () async {
              final amt = int.tryParse(amountCtrl.text.trim()) ?? 0;
              final cr = int.tryParse(creditsCtrl.text.trim()) ?? 0;
              if (amt == 0 && cr == 0) return;
              Navigator.pop(ctx);
              final res = await http.post(
                Uri.parse('${widget.serverUrl}/admin/add-balance/'),
                body: {
                  "admin_email": widget.adminEmail,
                  "admin_pin": widget.adminPin,
                  "target_email": targetUser['email'].toString(),
                  "amount": amt.toString(),
                  "credits": cr.toString(),
                  "note": noteCtrl.text.trim(),
                },
              );
              final data = jsonDecode(res.body);
              if (mounted) {
                ScaffoldMessenger.of(context).showSnackBar(
                  SnackBar(
                    content: Text(
                      data['message'] ?? data['detail'] ?? "Updated",
                    ),
                  ),
                );
              }
              _fetchAdminData();
            },
            child: const Text(
              "Update Balance",
              style: TextStyle(color: Colors.white),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _showCreatePromoDialog() async {
    TextEditingController codeCtrl = TextEditingController();
    TextEditingController creditsCtrl = TextEditingController(text: "1");
    TextEditingController maxUsesCtrl = TextEditingController(text: "10");

    await showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: const Text(
          "🎁 नया Promo Code बनाएं",
          style: TextStyle(color: Color(0xFFFFD600)),
        ),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: codeCtrl,
              textCapitalization: TextCapitalization.characters,
              decoration: const InputDecoration(
                labelText: "Promo Code (जैसे: DOSTFREE, VIP5)",
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: creditsCtrl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: "कितनी फ्री मूवी मिलेंगी? (Credits)",
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: maxUsesCtrl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: "कुल कितने लोग यूज़ कर सकते हैं?",
              ),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text("रद्द करें"),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFFFF007F),
            ),
            onPressed: () async {
              if (codeCtrl.text.trim().isEmpty) return;
              Navigator.pop(ctx);
              final res = await http.post(
                Uri.parse('${widget.serverUrl}/admin/create-promo/'),
                body: {
                  "admin_email": widget.adminEmail,
                  "admin_pin": widget.adminPin,
                  "code": codeCtrl.text.trim(),
                  "free_credits": creditsCtrl.text.trim(),
                  "max_uses": maxUsesCtrl.text.trim(),
                },
              );
              if (res.statusCode == 200 && mounted) {
                final data = jsonDecode(res.body);
                ScaffoldMessenger.of(context).showSnackBar(
                  SnackBar(content: Text(data['message'])),
                );
                _fetchAdminData();
              }
            },
            child: const Text("बनाएं", style: TextStyle(color: Colors.white)),
          )
        ],
      ),
    );
  }

  Future<void> _deletePromo(String code) async {
    await http.post(
      Uri.parse('${widget.serverUrl}/admin/delete-promo/'),
      body: {
        "admin_email": widget.adminEmail,
        "admin_pin": widget.adminPin,
        "code": code,
      },
    );
    _fetchAdminData();
  }

  Future<void> _approvePayment(String reqId) async {
    final res = await http.post(
      Uri.parse('${widget.serverUrl}/admin/approve-payment/'),
      body: {
        "admin_email": widget.adminEmail,
        "admin_pin": widget.adminPin,
        "req_id": reqId,
      },
    );
    if (res.statusCode == 200 && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text("✅ पेमेंट अप्रूव हो गई!")),
      );
      _fetchAdminData();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text("👑 Admin Control Panel"),
        backgroundColor: const Color(0xFF1A0E36),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: _fetchAdminData,
          )
        ],
      ),
      body: loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Row(
                  children: [
                    _statCard(
                      "कुल यूज़र",
                      "$totalInstalls",
                      const Color(0xFF00E5FF),
                    ),
                    const SizedBox(width: 8),
                    _statCard("कमाई", "₹$totalRevenue", Colors.greenAccent),
                  ],
                ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    _statCard(
                      "कुल वॉलेट ₹",
                      "₹$totalWallets",
                      Colors.lightGreenAccent,
                    ),
                    const SizedBox(width: 8),
                    _statCard(
                      "कुल मूवीज़",
                      "$totalMovies",
                      const Color(0xFFFFD600),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    _statCard(
                      "🎁 रेफरल्स",
                      "$totalReferrals",
                      const Color(0xFFFF007F),
                    ),
                    const SizedBox(width: 8),
                    _statCard(
                      "🎭 Face-Swaps",
                      "$totalFaceSwaps",
                      Colors.purpleAccent,
                    ),
                  ],
                ),
                const SizedBox(height: 20),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    const Text(
                      "🎟️ एक्टिव Promo Codes:",
                      style: TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.bold,
                        color: Color(0xFFFFD600),
                      ),
                    ),
                    ElevatedButton.icon(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF7000FF),
                      ),
                      onPressed: _showCreatePromoDialog,
                      icon:
                          const Icon(Icons.add, color: Colors.white, size: 18),
                      label: const Text(
                        "नया कोड",
                        style: TextStyle(color: Colors.white),
                      ),
                    ),
                  ],
                ),
                if (promoCodes.isEmpty)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 6),
                    child: Text(
                      "अभी कोई Promo Code नहीं बना है।",
                      style: TextStyle(color: Colors.white54),
                    ),
                  ),
                ...promoCodes.map(
                  (pc) => Card(
                    color: const Color(0xFF231342),
                    child: ListTile(
                      title: Text(
                        "${pc['code']} (${pc['free_credits']} फ्री मूवी)",
                        style: const TextStyle(
                          fontWeight: FontWeight.bold,
                          color: Color(0xFFFFD600),
                        ),
                      ),
                      subtitle: Text(
                        "यूज़ हुआ: ${pc['used_count']} / ${pc['max_uses']}",
                      ),
                      trailing: IconButton(
                        icon:
                            const Icon(Icons.delete, color: Colors.redAccent),
                        onPressed: () => _deletePromo(pc['code']),
                      ),
                    ),
                  ),
                ),
                const Divider(height: 28),
                const Text(
                  "💰 पेंडिंग ₹50 पेमेंट रिक्वेस्ट:",
                  style: TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.bold,
                    color: Color(0xFFFF007F),
                  ),
                ),
                if (pendingPayments.isEmpty)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 6),
                    child: Text(
                      "कोई पेंडिंग पेमेंट नहीं है।",
                      style: TextStyle(color: Colors.white54),
                    ),
                  ),
                ...pendingPayments.map(
                  (p) => Card(
                    color: const Color(0xFF28144A),
                    child: ListTile(
                      title: Text(
                        "${p['name']} (₹${p['amount']} - ${p['movies_count']} मूवी)",
                      ),
                      subtitle: Text(
                        "Google ID: ${p['email']}\nUTR: ${p['utr']}",
                      ),
                      trailing: ElevatedButton(
                        style: ElevatedButton.styleFrom(
                          backgroundColor: Colors.green,
                        ),
                        onPressed: () => _approvePayment(p['req_id']),
                        child: const Text(
                          "Approve",
                          style: TextStyle(color: Colors.white),
                        ),
                      ),
                    ),
                  ),
                ),
                const Divider(height: 28),
                const Text(
                  "👥 कस्टमर वॉलेट & पास कंट्रोल:",
                  style: TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.bold,
                    color: Color(0xFF00E5FF),
                  ),
                ),
                const SizedBox(height: 8),
                ...users.map(
                  (u) => Card(
                    color: const Color(0xFF1B1236),
                    child: ListTile(
                      leading: const CircleAvatar(
                        backgroundColor: Color(0xFFFF007F),
                        child: Icon(Icons.person, color: Colors.white),
                      ),
                      title: Text(u['name'] ?? "User"),
                      subtitle: Text(
                        "${u['email']}\nवॉलेट: ₹${u['wallet_balance'] ?? 0} | पास: ${u['credits'] ?? 0} | मूवी: ${u['movies_created'] ?? 0} | Ref: ${u['referral_code'] ?? '-'}",
                      ),
                      trailing: ElevatedButton(
                        style: ElevatedButton.styleFrom(
                          backgroundColor: const Color(0xFFFFD600),
                          padding: const EdgeInsets.symmetric(horizontal: 10),
                        ),
                        onPressed: () =>
                            _showAdjustWalletDialog(Map<String, dynamic>.from(u)),
                        child: const Text(
                          "₹ / पास",
                          style: TextStyle(
                            color: Colors.black,
                            fontWeight: FontWeight.bold,
                            fontSize: 12,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                const Divider(height: 28),
                const Text(
                  "🎁 Refer & Earn Tracking:",
                  style: TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.bold,
                    color: Color(0xFFFFD600),
                  ),
                ),
                if (referrals.isEmpty)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 6),
                    child: Text(
                      "अभी कोई रेफरल क्लेम नहीं हुआ है।",
                      style: TextStyle(color: Colors.white54),
                    ),
                  ),
                ...referrals.map(
                  (r) => Card(
                    color: const Color(0xFF231342),
                    child: ListTile(
                      title: Text(
                        "${r['referrer_email']} ➔ ${r['referee_email']}",
                        style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
                      ),
                      subtitle: Text(
                        "Code: ${r['code']} • +${r['reward_passes'] ?? 1} Pass Both • ${r['created_at'] ?? ''}",
                        style: const TextStyle(fontSize: 11, color: Colors.greenAccent),
                      ),
                    ),
                  ),
                ),
              ],
            ),
    );
  }

  Widget _statCard(String title, String val, Color color) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: const Color(0xFF1B1236),
          borderRadius: BorderRadius.circular(14),
          border: Border.all(color: color.withOpacity(0.5)),
        ),
        child: Column(
          children: [
            Text(
              val,
              style: TextStyle(
                fontSize: 20,
                fontWeight: FontWeight.bold,
                color: color,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              title,
              style: const TextStyle(fontSize: 12, color: Colors.white70),
            ),
          ],
        ),
      ),
    );
  }
}

// ==================== IPHONE FACE ID HANDS-FREE 5-ANGLE 3D SCANNER ====================

class HandsFreeFaceIdScannerPage extends StatefulWidget {
  const HandsFreeFaceIdScannerPage({super.key});

  @override
  State<HandsFreeFaceIdScannerPage> createState() =>
      _HandsFreeFaceIdScannerPageState();
}

class _HandsFreeFaceIdScannerPageState
    extends State<HandsFreeFaceIdScannerPage> {
  static const List<Map<String, String>> _steps = [
    {
      "key": "front",
      "arrow": "🎯",
      "hi": "🎯 सामने देखें (Look Straight)",
      "hint": "चेहरा सर्कल के बीच में रखें — Auto-Capture होगा",
    },
    {
      "key": "left",
      "arrow": "⬅️",
      "hi": "⬅️ थोड़ा बाएं मुड़ें (Turn Left)",
      "hint": "चेहरा धीरे से बाईं ओर घुमाएँ — बिना बटन दबाए कैप्चर होगा",
    },
    {
      "key": "right",
      "arrow": "➡️",
      "hi": "➡️ थोड़ा दाएं मुड़ें (Turn Right)",
      "hint": "चेहरा धीरे से दाईं ओर घुमाएँ — Auto-Capture होगा",
    },
    {
      "key": "up",
      "arrow": "⬆️",
      "hi": "⬆️ थोड़ा ऊपर देखें (Look Up)",
      "hint": "ठुड्डी हल्की सी ऊपर उठाएँ — Auto-Capture होगा",
    },
    {
      "key": "down",
      "arrow": "⬇️",
      "hi": "⬇️ थोड़ा नीचे देखें (Look Down)",
      "hint": "चेहरा हल्का सा नीचे झुकाएँ — 5वां और आखिरी एंगल",
    },
  ];

  CameraController? _cameraController;
  CameraDescription? _frontCamera;
  late final FaceDetector _faceDetector;
  final Map<String, File> _captured = {};

  int _currentStepIdx = 0;
  int _matchedStreak = 0;
  bool _isInitializing = true;
  bool _isProcessingFrame = false;
  bool _isCapturing = false;
  bool _faceDetected = false;
  double _liveYaw = 0;
  double _livePitch = 0;
  String? _cameraError;

  @override
  void initState() {
    super.initState();
    _faceDetector = FaceDetector(
      options: FaceDetectorOptions(
        enableTracking: true,
        performanceMode: FaceDetectorMode.fast,
      ),
    );
    _initFrontCamera();
  }

  Future<void> _initFrontCamera() async {
    try {
      final cams = await availableCameras();
      if (cams.isEmpty) {
        if (mounted) {
          setState(() {
            _isInitializing = false;
            _cameraError = "इस डिवाइस में कैमरा नहीं मिला।";
          });
        }
        return;
      }
      _frontCamera = cams.firstWhere(
        (c) => c.lensDirection == CameraLensDirection.front,
        orElse: () => cams.first,
      );
      _cameraController = CameraController(
        _frontCamera!,
        ResolutionPreset.medium,
        enableAudio: false,
        imageFormatGroup: Platform.isAndroid
            ? ImageFormatGroup.nv21
            : ImageFormatGroup.bgra8888,
      );
      await _cameraController!.initialize();
      if (!mounted) return;
      setState(() => _isInitializing = false);
      await _startLiveFaceTracking();
    } catch (e) {
      if (mounted) {
        setState(() {
          _isInitializing = false;
          _cameraError = "फ्रंट कैमरा शुरू करने में समस्या: $e";
        });
      }
    }
  }

  Future<void> _startLiveFaceTracking() async {
    final ctrl = _cameraController;
    if (ctrl == null || !ctrl.value.isInitialized) return;
    if (ctrl.value.isStreamingImages) return;
    try {
      await ctrl.startImageStream(_processCameraFrame);
    } catch (_) {}
  }

  InputImage? _toInputImage(CameraImage image) {
    final cam = _frontCamera;
    if (cam == null) return null;
    final rotation =
        InputImageRotationValue.fromRawValue(cam.sensorOrientation) ??
            InputImageRotation.rotation270deg;
    final format = InputImageFormatValue.fromRawValue(image.format.raw) ??
        (Platform.isAndroid
            ? InputImageFormat.nv21
            : InputImageFormat.bgra8888);

    final WriteBuffer allBytes = WriteBuffer();
    for (final Plane plane in image.planes) {
      allBytes.putUint8List(plane.bytes);
    }
    final bytes = allBytes.done().buffer.asUint8List();

    return InputImage.fromBytes(
      bytes: bytes,
      metadata: InputImageMetadata(
        size: Size(image.width.toDouble(), image.height.toDouble()),
        rotation: rotation,
        format: format,
        bytesPerRow: image.planes.first.bytesPerRow,
      ),
    );
  }

  bool _matchesAngle(String key, double yaw, double pitch) {
    switch (key) {
      case "front":
        return yaw.abs() <= 13.0 && pitch.abs() <= 13.0;
      case "left":
        return yaw >= 14.0;
      case "right":
        return yaw <= -14.0;
      case "up":
        return pitch >= 10.0;
      case "down":
        return pitch <= -10.0;
      default:
        return true;
    }
  }

  Future<void> _processCameraFrame(CameraImage image) async {
    if (_isProcessingFrame || _isCapturing || _captured.length >= 5) return;
    _isProcessingFrame = true;
    try {
      final inputImage = _toInputImage(image);
      if (inputImage == null) {
        _isProcessingFrame = false;
        return;
      }
      final faces = await _faceDetector.processImage(inputImage);
      if (!mounted) return;

      if (faces.isEmpty) {
        if (_faceDetected || _matchedStreak > 0) {
          setState(() {
            _faceDetected = false;
            _matchedStreak = 0;
          });
        }
      } else {
        final face = faces.first;
        final double yaw = face.headEulerAngleY ?? 0.0;
        final double pitch = face.headEulerAngleX ?? 0.0;
        final String targetKey = _steps[_currentStepIdx]["key"]!;
        final bool matched = _matchesAngle(targetKey, yaw, pitch);

        setState(() {
          _faceDetected = true;
          _liveYaw = yaw;
          _livePitch = pitch;
          _matchedStreak = matched ? (_matchedStreak + 1) : 0;
        });

        // 2 consecutive steady frames -> 100% Hands-Free Auto-Capture!
        if (_matchedStreak >= 2 && !_isCapturing) {
          await _captureCurrentAngle(auto: true);
        }
      }
    } catch (_) {
    } finally {
      _isProcessingFrame = false;
    }
  }

  Future<void> _captureCurrentAngle({bool auto = false}) async {
    if (_isCapturing || _cameraController == null) return;
    if (!_cameraController!.value.isInitialized) return;
    setState(() {
      _isCapturing = true;
      _matchedStreak = 0;
    });

    try {
      if (_cameraController!.value.isStreamingImages) {
        await _cameraController!.stopImageStream();
      }
      final XFile shot = await _cameraController!.takePicture();
      final File uprightFile = await _fixUprightPortrait(
        File(shot.path),
        sensorOrientation: _frontCamera?.sensorOrientation,
        isFrontCamera: true,
      );
      final String angleKey = _steps[_currentStepIdx]["key"]!;

      await HapticFeedback.heavyImpact();
      await SystemSound.play(SystemSoundType.click);

      if (!mounted) return;
      setState(() {
        _captured[angleKey] = uprightFile;
        // Advance to next uncaptured angle
        for (int i = 0; i < _steps.length; i++) {
          if (!_captured.containsKey(_steps[i]["key"]!)) {
            _currentStepIdx = i;
            break;
          }
        }
      });

      if (_captured.length >= 5) {
        await Future.delayed(const Duration(milliseconds: 700));
        if (mounted) {
          Navigator.pop(context, Map<String, File>.from(_captured));
        }
        return;
      }

      await Future.delayed(const Duration(milliseconds: 350));
      if (mounted) {
        setState(() => _isCapturing = false);
        await _startLiveFaceTracking();
      }
    } catch (_) {
      if (mounted) {
        setState(() => _isCapturing = false);
        await _startLiveFaceTracking();
      }
    }
  }

  @override
  void dispose() {
    _cameraController?.dispose();
    _faceDetector.close();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final step = _steps[_currentStepIdx];
    final bool allDone = _captured.length >= 5;
    final List<bool> completedSegments =
        _steps.map((s) => _captured.containsKey(s["key"]!)).toList();

    return Scaffold(
      backgroundColor: const Color(0xFF0A0614),
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        elevation: 0,
        title: const Text(
          "🎭 3D Face ID Auto-Scan",
          style: TextStyle(
            color: Color(0xFFFFD600),
            fontWeight: FontWeight.bold,
            fontSize: 16,
          ),
        ),
        actions: [
          Center(
            child: Padding(
              padding: const EdgeInsets.only(right: 16),
              child: Text(
                "${_captured.length}/5",
                style: TextStyle(
                  color: allDone
                      ? const Color(0xFF00E676)
                      : const Color(0xFF00E5FF),
                  fontWeight: FontWeight.w900,
                  fontSize: 15,
                ),
              ),
            ),
          ),
        ],
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
          child: Column(
            children: [
              // Directional Prompt Banner
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(
                  color: allDone
                      ? const Color(0xFF00E676).withOpacity(0.16)
                      : const Color(0xFF191030),
                  borderRadius: BorderRadius.circular(16),
                  border: Border.all(
                    color: allDone
                        ? const Color(0xFF00E676)
                        : (_faceDetected
                            ? const Color(0xFF00E5FF)
                            : const Color(0xFFFF007F)),
                  ),
                ),
                child: Column(
                  children: [
                    Text(
                      allDone
                          ? "✅ 5-Angle 3D Face Profile Ready!"
                          : step["hi"]!,
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: allDone
                            ? const Color(0xFF00E676)
                            : Colors.white,
                        fontSize: 17,
                        fontWeight: FontWeight.w900,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      allDone
                          ? "सभी 5 एंगल Upright Portrait में लॉक हो गए!"
                          : step["hint"]!,
                      textAlign: TextAlign.center,
                      style: const TextStyle(
                        color: Colors.white70,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 22),

              // Live Circular Front Camera Preview + 5-Segment Green Face ID Ring
              Expanded(
                child: Center(
                  child: _isInitializing
                      ? const CircularProgressIndicator(
                          color: Color(0xFF00E5FF),
                        )
                      : _cameraError != null
                          ? Column(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                const Icon(
                                  Icons.videocam_off,
                                  color: Colors.redAccent,
                                  size: 48,
                                ),
                                const SizedBox(height: 12),
                                Text(
                                  _cameraError!,
                                  textAlign: TextAlign.center,
                                  style: const TextStyle(
                                    color: Colors.white70,
                                    fontSize: 13,
                                  ),
                                ),
                              ],
                            )
                          : SizedBox(
                              width: 280,
                              height: 280,
                              child: Stack(
                                alignment: Alignment.center,
                                children: [
                                  CustomPaint(
                                    size: const Size(280, 280),
                                    painter: _FaceIdRingPainter(
                                      completedSegments: completedSegments,
                                      activeIndex: _currentStepIdx,
                                      faceDetected: _faceDetected,
                                    ),
                                  ),
                                  ClipOval(
                                    child: SizedBox(
                                      width: 244,
                                      height: 244,
                                      child: FittedBox(
                                        fit: BoxFit.cover,
                                        child: SizedBox(
                                          width: _cameraController!
                                                  .value.previewSize?.height ??
                                              480,
                                          height: _cameraController!
                                                  .value.previewSize?.width ??
                                              640,
                                          child: CameraPreview(
                                            _cameraController!,
                                          ),
                                        ),
                                      ),
                                    ),
                                  ),
                                  if (!allDone)
                                    Positioned(
                                      bottom: 14,
                                      child: Container(
                                        padding: const EdgeInsets.symmetric(
                                          horizontal: 12,
                                          vertical: 5,
                                        ),
                                        decoration: BoxDecoration(
                                          color: Colors.black.withOpacity(0.72),
                                          borderRadius:
                                              BorderRadius.circular(20),
                                          border: Border.all(
                                            color: _faceDetected
                                                ? const Color(0xFF00E676)
                                                : Colors.white30,
                                          ),
                                        ),
                                        child: Text(
                                          _isCapturing
                                              ? "📸 Auto-Capturing..."
                                              : (_faceDetected
                                                  ? "${step['arrow']} Face Locked · Yaw ${_liveYaw.toStringAsFixed(0)}°"
                                                  : "चेहरा सर्कल के अंदर लाएँ"),
                                          style: TextStyle(
                                            color: _faceDetected
                                                ? const Color(0xFF00E676)
                                                : Colors.white70,
                                            fontSize: 11,
                                            fontWeight: FontWeight.bold,
                                          ),
                                        ),
                                      ),
                                    ),
                                ],
                              ),
                            ),
                ),
              ),

              const SizedBox(height: 14),
              // 5 Angle Step Pills
              Wrap(
                spacing: 6,
                runSpacing: 6,
                alignment: WrapAlignment.center,
                children: List.generate(_steps.length, (i) {
                  final s = _steps[i];
                  final done = _captured.containsKey(s["key"]!);
                  final active = i == _currentStepIdx;
                  return GestureDetector(
                    onTap: () => setState(() => _currentStepIdx = i),
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 10,
                        vertical: 6,
                      ),
                      decoration: BoxDecoration(
                        color: done
                            ? const Color(0xFF00E676).withOpacity(0.22)
                            : (active
                                ? const Color(0xFFFF007F).withOpacity(0.25)
                                : const Color(0xFF1A1132)),
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(
                          color: done
                              ? const Color(0xFF00E676)
                              : (active
                                  ? const Color(0xFFFF007F)
                                  : Colors.white24),
                        ),
                      ),
                      child: Text(
                        "${done ? '✓ ' : '${s['arrow']} '}${s['key']!.toUpperCase()}",
                        style: TextStyle(
                          color: done ? const Color(0xFF00E676) : Colors.white,
                          fontSize: 11,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ),
                  );
                }),
              ),
              const SizedBox(height: 14),

              // Bottom Row: Fallback manual tap + Save
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      style: OutlinedButton.styleFrom(
                        side: const BorderSide(color: Colors.white24),
                        padding: const EdgeInsets.symmetric(vertical: 12),
                      ),
                      onPressed: (_isCapturing || _cameraController == null)
                          ? null
                          : () => _captureCurrentAngle(auto: false),
                      icon: const Icon(
                        Icons.camera,
                        color: Color(0xFF00E5FF),
                        size: 18,
                      ),
                      label: const Text(
                        "Manual Tap (Optional)",
                        style: TextStyle(color: Colors.white70, fontSize: 12),
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: ElevatedButton(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: allDone
                            ? const Color(0xFF00E676)
                            : const Color(0xFFFF007F),
                        padding: const EdgeInsets.symmetric(vertical: 12),
                      ),
                      onPressed: _captured.isEmpty
                          ? null
                          : () => Navigator.pop(
                                context,
                                Map<String, File>.from(_captured),
                              ),
                      child: Text(
                        allDone
                            ? "✅ Done (5/5 Ready)"
                            : "Save (${_captured.length}/5)",
                        style: TextStyle(
                          color: allDone ? Colors.black : Colors.white,
                          fontWeight: FontWeight.w900,
                          fontSize: 13,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),
            ],
          ),
        ),
      ),
    );
  }
}

class _FaceIdRingPainter extends CustomPainter {
  final List<bool> completedSegments;
  final int activeIndex;
  final bool faceDetected;

  _FaceIdRingPainter({
    required this.completedSegments,
    required this.activeIndex,
    required this.faceDetected,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final radius = (size.width / 2) - 8;
    final rect = Rect.fromCircle(center: center, radius: radius);
    const int total = 5;
    const double gap = 0.14;
    final double sweep = (2 * math.pi / total) - gap;

    for (int i = 0; i < total; i++) {
      final startAngle = -math.pi / 2 + i * (2 * math.pi / total) + gap / 2;
      final bool isDone =
          i < completedSegments.length && completedSegments[i];
      final bool isActive = i == activeIndex;
      final paint = Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = isDone ? 9.0 : (isActive ? 8.0 : 5.5)
        ..strokeCap = StrokeCap.round
        ..color = isDone
            ? const Color(0xFF00E676)
            : (isActive
                ? (faceDetected
                    ? const Color(0xFF00E5FF)
                    : const Color(0xFFFF007F))
                : Colors.white24);
      canvas.drawArc(rect, startAngle, sweep, false, paint);
    }
  }

  @override
  bool shouldRepaint(covariant _FaceIdRingPainter oldDelegate) => true;
}

