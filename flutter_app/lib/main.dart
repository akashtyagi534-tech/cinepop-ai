import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:path_provider/path_provider.dart';
import 'package:video_player/video_player.dart';
import 'package:chewie/chewie.dart';
import 'package:google_sign_in/google_sign_in.dart';

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
  String serverUrl = "https://YOUR-RAILWAY-APP.up.railway.app";
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
  final TextEditingController loginReferralController = TextEditingController();
  final TextEditingController applyReferralController = TextEditingController();
  int selectedMinutes = 30;
  List<Map<String, dynamic>> characters = [];
  String dataEstimateText = "डेटा हिसाब: ~480 MB";
  bool isSubmitting = false;

  // Active Movie Project & Studio Actions
  String? activeProjectId;
  int progress = 0;
  String statusMessage = "";
  List<dynamic> readyParts = [];
  Map<String, dynamic> scriptsData = {};
  bool isCompleted = false;
  bool generatingTrailer = false;
  bool generatingPoster = false;
  bool dubbingMovie = false;
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
    if (savedUrl != null && savedUrl.isNotEmpty) {
      serverUrl = savedUrl;
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
    loginReferralController.dispose();
    applyReferralController.dispose();
    fsTitleController.dispose();
    fsDubController.dispose();
    super.dispose();
  }

  Future<void> _handleGoogleSignIn() async {
    try {
      final account = await _googleSignIn.signIn();
      if (account != null) {
        await _completeLogin(
          account.email,
          account.displayName ?? "CinePop User",
          account.photoUrl ?? "",
        );
        return;
      }
    } catch (_) {}
    if (mounted) {
      _showQuickGoogleIdDialog();
    }
  }

  Future<void> _showQuickGoogleIdDialog() async {
    await showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: const Text(
          "🔐 Google ID से लॉगिन करें",
          style: TextStyle(color: Color(0xFFFFD600)),
        ),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: manualEmailController,
              keyboardType: TextInputType.emailAddress,
              decoration: const InputDecoration(
                border: OutlineInputBorder(),
                labelText: "अपनी Gmail ID डालें",
                hintText: "example@gmail.com",
              ),
            ),
            const SizedBox(height: 10),
            TextField(
              controller: loginReferralController,
              textCapitalization: TextCapitalization.characters,
              decoration: const InputDecoration(
                border: OutlineInputBorder(),
                labelText: "🎁 दोस्त का Referral Code (Optional)",
                hintText: "जैसे: CPABC123 (+1 Free Movie Pass)",
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
              final em = manualEmailController.text.trim().toLowerCase();
              if (em.contains("@")) {
                Navigator.pop(ctx);
                await _completeLogin(em, em.split("@")[0], "");
              }
            },
            child: const Text(
              "लॉगिन करें",
              style: TextStyle(color: Colors.white),
            ),
          )
        ],
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
    double uploadMb = characters.length * 1.5;
    double downloadMb = selectedMinutes * 16.0;
    double totalGb = (uploadMb + downloadMb) / 1024;
    int parts = (selectedMinutes / 30).ceil();
    setState(() {
      dataEstimateText =
          "डेटा खर्च: ${(uploadMb + downloadMb).toStringAsFixed(0)} MB (~${totalGb.toStringAsFixed(2)} GB) | पार्ट्स: $parts";
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

  Future<void> _addCharacter() async {
    final picker = ImagePicker();
    final XFile? photo =
        await picker.pickImage(source: ImageSource.gallery, imageQuality: 85);
    if (photo == null) return;

    TextEditingController nameCtrl = TextEditingController();
    String selectedVoice = "hi-IN-MadhurNeural";
    String selectedPitch = "normal";

    if (!mounted) return;
    await showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: const Text(
          "🎭 कैरेक्टर जोड़ें",
          style: TextStyle(color: Color(0xFFFFD600)),
        ),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: nameCtrl,
                decoration: const InputDecoration(
                  labelText: "नाम (जैसे: Hero, Villain)",
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
              if (nameCtrl.text.isNotEmpty) {
                setState(() {
                  characters.add({
                    "name": nameCtrl.text.trim(),
                    "voice": selectedVoice,
                    "pitch": selectedPitch,
                    "file": File(photo.path),
                  });
                });
                _updateDataEstimate();
                Navigator.pop(ctx);
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
      request.fields['duration_minutes'] = selectedMinutes.toString();
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

      for (var c in characters) {
        request.files.add(
          await http.MultipartFile.fromPath('images', (c['file'] as File).path),
        );
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
    syncTimer = Timer.periodic(const Duration(seconds: 8), (_) async {
      if (activeProjectId == null) return;
      try {
        final res = await http
            .get(Uri.parse('$serverUrl/sync-project/$activeProjectId'))
            .timeout(const Duration(seconds: 8));
        if (res.statusCode == 200) {
          final data = jsonDecode(res.body);
          setState(() {
            isServerOnline = true;
            progress = data['progress'] ?? 0;
            statusMessage = data['status_text'] ?? "";
            readyParts = data['ready_parts'] ?? [];
            scriptsData = data['scripts'] ?? {};
            isCompleted = data['is_completed'] ?? false;
          });
          if (isCompleted) syncTimer?.cancel();
        }
      } catch (_) {
        setState(
          () => statusMessage =
              "ऑफलाइन मोड: क्लाउड पर मूवी बन रही है। नेट ऑन होते ही अपडेट दिखेगा...",
        );
      }
    });
  }

  Future<void> _playOnlinePreview(String fileType) async {
    try {
      final streamUrl = "$serverUrl/stream/$activeProjectId/$fileType";
      _videoController?.dispose();
      _chewieController?.dispose();

      _videoController = VideoPlayerController.networkUrl(Uri.parse(streamUrl));
      await _videoController!.initialize();
      _chewieController = ChewieController(
        videoPlayerController: _videoController!,
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

  Future<void> _downloadVideo(String fileType) async {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text("$fileType डाउनलोड शुरू हो रहा है...")),
    );
    try {
      final dir = await getExternalStorageDirectory() ??
          await getApplicationDocumentsDirectory();
      final savePath = "${dir.path}/CinePop_${activeProjectId}_$fileType.mp4";
      final file = File(savePath);
      final sink = file.openWrite();

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
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text("✅ सेव हो गया: $savePath")),
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

  Future<void> _downloadDirectUrl(String url, String fileName) async {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text("$fileName डाउनलोड शुरू हो रहा है...")),
    );
    try {
      final dir = await getExternalStorageDirectory() ??
          await getApplicationDocumentsDirectory();
      final savePath = "${dir.path}/$fileName";
      final file = File(savePath);
      final sink = file.openWrite();
      final response = await http.Request('GET', Uri.parse(url)).send();
      if (response.statusCode == 200) {
        await for (var chunk in response.stream) {
          sink.add(chunk);
        }
        await sink.close();
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text("✅ सेव हो गया: $savePath")),
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
                  const SizedBox(height: 36),
                  ElevatedButton.icon(
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.white,
                      foregroundColor: Colors.black,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 28,
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
                        fontSize: 17,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                  const SizedBox(height: 16),
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
              CircleAvatar(
                backgroundImage: (userPhoto != null && userPhoto!.isNotEmpty)
                    ? NetworkImage(userPhoto!)
                    : null,
                child: (userPhoto == null || userPhoto!.isEmpty)
                    ? const Icon(Icons.person)
                    : null,
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
        const SizedBox(height: 16),

        // 1. Runtime
        const Text(
          "1. मूवी की लंबाई चुनें:",
          style: TextStyle(
            fontWeight: FontWeight.bold,
            color: Color(0xFF00E5FF),
          ),
        ),
        DropdownButton<int>(
          value: selectedMinutes,
          isExpanded: true,
          dropdownColor: const Color(0xFF1B1236),
          items: const [
            DropdownMenuItem(value: 30, child: Text("30 मिनट (1 पार्ट)")),
            DropdownMenuItem(
              value: 60,
              child: Text("1 घंटा (2 पार्ट्स + फुल मूवी)"),
            ),
            DropdownMenuItem(
              value: 90,
              child: Text("1.5 घंटा (3 पार्ट्स + फुल मूवी)"),
            ),
            DropdownMenuItem(
              value: 120,
              child: Text("2 घंटे (4 पार्ट्स + फुल मूवी)"),
            ),
            DropdownMenuItem(
              value: 180,
              child: Text("3 घंटे (6 पार्ट्स + फुल मूवी)"),
            ),
          ],
          onChanged: (val) {
            selectedMinutes = val!;
            _updateDataEstimate();
          },
        ),
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

        // 2. Character Photos
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            const Text(
              "2. कैरेक्टर फोटोज़:",
              style: TextStyle(
                fontWeight: FontWeight.bold,
                color: Color(0xFF00E5FF),
              ),
            ),
            ElevatedButton.icon(
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFF7000FF),
              ),
              onPressed: _addCharacter,
              icon: const Icon(Icons.add_a_photo, color: Colors.white),
              label: const Text(
                "कैरेक्टर जोड़ें",
                style: TextStyle(color: Colors.white),
              ),
            ),
          ],
        ),
        Wrap(
          spacing: 8,
          children: characters
              .map(
                (c) => Chip(
                  backgroundColor: const Color(0xFF281854),
                  avatar: CircleAvatar(
                    backgroundImage: FileImage(c['file'] as File),
                  ),
                  label: Text(
                    "${c['name']} (${c['pitch'] ?? 'normal'})",
                  ),
                  onDeleted: () {
                    setState(() => characters.remove(c));
                    _updateDataEstimate();
                  },
                ),
              )
              .toList(),
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
        Text("प्रोजेक्ट ID: $activeProjectId", textAlign: TextAlign.center),
        const SizedBox(height: 8),
        LinearProgressIndicator(
          value: progress / 100,
          minHeight: 12,
          color: const Color(0xFFFF007F),
          backgroundColor: const Color(0xFF281854),
        ),
        const SizedBox(height: 8),
        Text(
          statusMessage,
          textAlign: TextAlign.center,
          style: const TextStyle(
            fontSize: 15,
            fontWeight: FontWeight.bold,
            color: Color(0xFFFFD600),
          ),
        ),
        const SizedBox(height: 16),
        if (_chewieController != null &&
            _videoController!.value.isInitialized) ...[
          Text(
            "प्रिव्यू चल रहा है: $currentlyPlayingType",
            style: const TextStyle(
              fontWeight: FontWeight.bold,
              color: Color(0xFF00E5FF),
            ),
          ),
          const SizedBox(height: 8),
          AspectRatio(
            aspectRatio: _videoController!.value.aspectRatio,
            child: Chewie(controller: _chewieController!),
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
                "HD वीडियो डाउनलोड करें",
                style: TextStyle(color: Colors.white),
              ),
            ),
          ),
          const Divider(height: 24),
        ],
        const Text(
          "तैयार पार्ट्स (पहले Play करके चेक करें, फिर Download करें):",
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 8),
        ...readyParts.map(
          (p) => Card(
            color: const Color(0xFF1B1236),
            child: ListTile(
              title: Text("पार्ट $p (30 मिनट)"),
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
            backgroundColor: const Color(0xFFFF007F),
            padding: const EdgeInsets.symmetric(vertical: 12),
          ),
          onPressed: generatingTrailer ? null : _generateTrailerAndReel,
          icon: const Icon(Icons.movie_filter, color: Colors.white),
          label: Text(
            generatingTrailer
                ? "60-Sec Trailer & 9:16 Reel बन रहे हैं..."
                : "🎬 Generate 60-Sec Trailer / 9:16 Reel",
            style: const TextStyle(
              color: Colors.white,
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
        TextButton(
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
          child: const Text(
            "नया प्रोजेक्ट शुरू करें",
            style: TextStyle(color: Color(0xFF00E5FF)),
          ),
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
                trailing: Wrap(
                  spacing: 4,
                  children: [
                    IconButton(
                      icon: const Icon(Icons.photo_album, color: Color(0xFFFFD600)),
                      tooltip: "Movie Poster",
                      onPressed: () =>
                          _generateAndShowPoster(m['project_id'].toString()),
                    ),
                    IconButton(
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
                  style: const TextStyle(fontSize: 12, color: Colors.white70),
                ),
                trailing: j['status'] == 'ready'
                    ? Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          IconButton(
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
                            icon: const Icon(
                              Icons.download,
                              color: Colors.greenAccent,
                            ),
                            onPressed: () => _downloadDirectUrl(
                              '$serverUrl/face-swap/download/${j['job_id']}',
                              'CinePop_FaceSwap_${j['job_id']}.mp4',
                            ),
                          ),
                        ],
                      )
                    : const Icon(Icons.hourglass_top, color: Colors.amber),
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
