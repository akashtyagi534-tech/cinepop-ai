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
  bool isAdmin = false;
  String adminUpiId = "8269045143@ybl";
  int moviePrice = 50;

  final TextEditingController ideaController = TextEditingController();
  final TextEditingController serverController = TextEditingController();
  final TextEditingController manualEmailController = TextEditingController();
  int selectedMinutes = 60;
  List<Map<String, dynamic>> characters = [];
  String dataEstimateText = "डेटा हिसाब: ~960 MB";
  bool isSubmitting = false;

  String? activeProjectId;
  int progress = 0;
  String statusMessage = "";
  List<dynamic> readyParts = [];
  Map<String, dynamic> scriptsData = {};
  bool isCompleted = false;
  Timer? syncTimer;

  VideoPlayerController? _videoController;
  ChewieController? _chewieController;
  String? currentlyPlayingType;

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
      setState(() => isServerOnline = (res.statusCode == 200));
    } catch (_) {
      setState(() => isServerOnline = false);
    }
  }

  @override
  void dispose() {
    syncTimer?.cancel();
    _videoController?.dispose();
    _chewieController?.dispose();
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
        content: TextField(
          controller: manualEmailController,
          keyboardType: TextInputType.emailAddress,
          decoration: const InputDecoration(
            border: OutlineInputBorder(),
            labelText: "अपनी Gmail ID डालें",
            hintText: "example@gmail.com",
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
    });

    await _syncUserWithBackend(email, name, photo);
  }

  Future<void> _syncUserWithBackend(
    String email,
    String name,
    String photo,
  ) async {
    try {
      final res = await http.post(
        Uri.parse('$serverUrl/auth/google-login/'),
        body: {
          "email": email,
          "name": name,
          "photo_url": photo,
          "device_id": Platform.operatingSystem,
        },
      ).timeout(const Duration(seconds: 8));
      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        setState(() {
          isServerOnline = true;
          userCredits = data['user']['credits'] ?? 0;
          isAdmin = data['is_admin'] ?? false;
          adminUpiId = data['upi_id'] ?? "8269045143@ybl";
          moviePrice = data['movie_price'] ?? 50;
        });
      }
    } catch (_) {}
  }

  Future<void> _logout() async {
    try {
      await _googleSignIn.signOut();
    } catch (_) {}
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('user_email');
    setState(() {
      userEmail = null;
      isAdmin = false;
      userCredits = 0;
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
              final cleaned = serverController.text.trim().replaceAll(RegExp(r'/+$'), '');
              final prefs = await SharedPreferences.getInstance();
              await prefs.setString('server_url', cleaned);
              setState(() => serverUrl = cleaned);
              await _checkServerHealth();
              if (userEmail != null) {
                await _syncUserWithBackend(userEmail!, userName!, userPhoto ?? "");
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
    TextEditingController utrCtrl = TextEditingController();
    TextEditingController promoCtrl = TextEditingController();
    int qty = 1;

    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setModalState) => AlertDialog(
          backgroundColor: const Color(0xFF1B1236),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
          title: const Text(
            "🎟️ मूवी पास / Promo Code",
            style: TextStyle(
              color: Color(0xFFFFD600),
              fontWeight: FontWeight.bold,
            ),
          ),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
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
                        style: TextStyle(fontSize: 12, color: Color(0xFFFFD600)),
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
                                      data['new_credits'] ?? userCredits,
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
                const Divider(height: 24),
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: Colors.black45,
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: const Color(0xFF00E5FF)),
                  ),
                  child: Column(
                    children: [
                      const Text(
                        "या UPI से ₹50 प्रति मूवी पेमेंट करें:",
                        style: TextStyle(fontSize: 12),
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
                              Clipboard.setData(ClipboardData(text: adminUpiId));
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

  Future<void> _addCharacter() async {
    final picker = ImagePicker();
    final XFile? photo =
        await picker.pickImage(source: ImageSource.gallery, imageQuality: 85);
    if (photo == null) return;

    TextEditingController nameCtrl = TextEditingController();
    String selectedVoice = "hi-IN-MadhurNeural";

    await showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1B1236),
        title: const Text(
          "🎭 कैरेक्टर जोड़ें",
          style: TextStyle(color: Color(0xFFFFD600)),
        ),
        content: Column(
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
                  child: Text("लड़का (हिंदी)"),
                ),
                DropdownMenuItem(
                  value: "hi-IN-SwaraNeural",
                  child: Text("लड़की (हिंदी)"),
                ),
                DropdownMenuItem(
                  value: "en-IN-PrabhatNeural",
                  child: Text("भारी / कार्टून आवाज़"),
                ),
              ],
              onChanged: (val) => selectedVoice = val!,
              decoration: const InputDecoration(labelText: "आवाज़ चुनें"),
            )
          ],
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
    if (!isAdmin && userCredits < 1) {
      _showBuyCreditDialog();
      return;
    }

    if (ideaController.text.trim().isEmpty || characters.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text("कहानी और कम से कम 1 कैरेक्टर की फोटो ज़रूरी है!"),
        ),
      );
      return;
    }

    setState(() {
      isSubmitting = true;
      statusMessage = "फोटोज़ और कहानी क्लाउड पर भेजी जा रही है...";
    });

    try {
      var request = http.MultipartRequest(
        'POST',
        Uri.parse('$serverUrl/start-autonomous-movie/'),
      );
      request.fields['user_email'] = userEmail!;
      request.fields['idea'] = ideaController.text.trim();
      request.fields['duration_minutes'] = selectedMinutes.toString();
      request.fields['char_names'] =
          characters.map((c) => c['name']).join(',');
      request.fields['voices'] = characters.map((c) => c['voice']).join(',');

      for (var c in characters) {
        request.files.add(
          await http.MultipartFile.fromPath('images', (c['file'] as File).path),
        );
      }

      var streamedRes =
          await request.send().timeout(const Duration(seconds: 45));
      var res = await http.Response.fromStream(streamedRes);

      if (res.statusCode == 200) {
        var data = jsonDecode(res.body);
        final prefs = await SharedPreferences.getInstance();
        await prefs.setString('active_project_id', data['project_id']);

        if (!isAdmin) {
          setState(() => userCredits -= 1);
        }

        setState(() {
          activeProjectId = data['project_id'];
          statusMessage = data['status_text'];
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
                  if (res.statusCode == 200) {
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
                    "फोटो डालो, कहानी बताओ — पूरी 3 घंटे की मूवी तैयार!\nसिर्फ ₹50 प्रति मूवी",
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
              child: Image.asset('assets/logo.png', width: 32, height: 32),
            ),
            const SizedBox(width: 8),
            const Text(
              "CinePop AI",
              style: TextStyle(
                fontSize: 20,
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
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
              decoration: BoxDecoration(
                gradient: const LinearGradient(
                  colors: [Color(0xFFFF007F), Color(0xFF7000FF)],
                ),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Center(
                child: Text(
                  isAdmin ? "👑 FREE UNLIMITED" : "🎟️ $userCredits मूवी",
                  style: const TextStyle(
                    fontWeight: FontWeight.bold,
                    fontSize: 11,
                  ),
                ),
              ),
            ),
          ),
          if (isAdmin)
            IconButton(
              icon: const Icon(
                Icons.admin_panel_settings,
                color: Color(0xFF00E5FF),
              ),
              tooltip: "Admin Dashboard",
              onPressed: () {
                Navigator.push(
                  context,
                  MaterialPageRoute(
                    builder: (_) => AdminDashboardScreen(
                      serverUrl: serverUrl,
                      adminEmail: userEmail!,
                    ),
                  ),
                );
              },
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
        child: activeProjectId != null
            ? _buildStudioScreen()
            : _buildSetupScreen(),
      ),
    );
  }

  Widget _buildSetupScreen() {
    return ListView(
      children: [
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
                backgroundImage:
                    (userPhoto != null && userPhoto!.isNotEmpty)
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
                      userEmail!,
                      style: const TextStyle(
                        fontSize: 11,
                        color: Colors.white60,
                      ),
                    ),
                  ],
                ),
              ),
              if (!isAdmin)
                ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFFFFD600),
                  ),
                  onPressed: _showBuyCreditDialog,
                  child: const Text(
                    "₹50 / Promo",
                    style: TextStyle(
                      color: Colors.black,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                )
            ],
          ),
        ),
        const SizedBox(height: 16),
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
                  label: Text(c['name'] as String),
                  onDeleted: () {
                    setState(() => characters.remove(c));
                    _updateDataEstimate();
                  },
                ),
              )
              .toList(),
        ),
        const SizedBox(height: 16),
        TextField(
          controller: ideaController,
          maxLines: 4,
          decoration: InputDecoration(
            filled: true,
            fillColor: const Color(0xFF1B1236),
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(14),
            ),
            labelText: "3. पूरी कहानी का आइडिया लिखें",
            hintText:
                "जैसे: एक देसी सुपरहीरो जो अपने गाँव को एलियंस से बचाता है...",
          ),
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
                    ? "🚀 OK - मूवी बनाना शुरू करें (Admin 100% Free)"
                    : "🎬 OK - मूवी बनाएं (1 पास / ₹50)"),
            style: const TextStyle(
              fontSize: 16,
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
                  icon: const Icon(Icons.edit, color: Colors.white),
                  label: const Text(
                    "डायलॉग बदलें",
                    style: TextStyle(color: Colors.white),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: ElevatedButton.icon(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.green,
                  ),
                  onPressed: () => _downloadVideo(currentlyPlayingType!),
                  icon: const Icon(Icons.download, color: Colors.white),
                  label: const Text(
                    "डाउनलोड करें",
                    style: TextStyle(color: Colors.white),
                  ),
                ),
              ),
            ],
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
        const SizedBox(height: 30),
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
}

class AdminDashboardScreen extends StatefulWidget {
  final String serverUrl;
  final String adminEmail;
  const AdminDashboardScreen({
    super.key,
    required this.serverUrl,
    required this.adminEmail,
  });

  @override
  State<AdminDashboardScreen> createState() => _AdminDashboardScreenState();
}

class _AdminDashboardScreenState extends State<AdminDashboardScreen> {
  int totalInstalls = 0;
  int totalRevenue = 0;
  int totalMovies = 0;
  List<dynamic> users = [];
  List<dynamic> pendingPayments = [];
  List<dynamic> promoCodes = [];
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
          '${widget.serverUrl}/admin/stats/?admin_email=${widget.adminEmail}',
        ),
      );
      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        setState(() {
          totalInstalls = data['total_installs'] ?? 0;
          totalRevenue = data['total_revenue'] ?? 0;
          totalMovies = data['total_movies_made'] ?? 0;
          users = data['users'] ?? [];
          pendingPayments = data['pending_payments'] ?? [];
          promoCodes = data['promo_codes'] ?? [];
        });
      }
    } catch (_) {}
    setState(() => loading = false);
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
                  "code": codeCtrl.text.trim(),
                  "free_credits": creditsCtrl.text.trim(),
                  "max_uses": maxUsesCtrl.text.trim(),
                },
              );
              if (res.statusCode == 200) {
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
      body: {"admin_email": widget.adminEmail, "code": code},
    );
    _fetchAdminData();
  }

  Future<void> _approvePayment(String reqId) async {
    final res = await http.post(
      Uri.parse('${widget.serverUrl}/admin/approve-payment/'),
      body: {"admin_email": widget.adminEmail, "req_id": reqId},
    );
    if (res.statusCode == 200) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text("✅ पेमेंट अप्रूव हो गई!")),
      );
      _fetchAdminData();
    }
  }

  Future<void> _grantCredit(String targetEmail) async {
    final res = await http.post(
      Uri.parse('${widget.serverUrl}/admin/add-free-credit/'),
      body: {
        "admin_email": widget.adminEmail,
        "target_email": targetEmail,
        "credits": "1",
      },
    );
    if (res.statusCode == 200) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text("✅ $targetEmail को 1 फ्री मूवी पास मिल गया!"),
        ),
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
                      "कुल इंस्टॉल",
                      "$totalInstalls",
                      const Color(0xFF00E5FF),
                    ),
                    const SizedBox(width: 8),
                    _statCard("कुल कमाई", "₹$totalRevenue", Colors.greenAccent),
                    const SizedBox(width: 8),
                    _statCard(
                      "कुल मूवीज़",
                      "$totalMovies",
                      const Color(0xFFFFD600),
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
                      icon: const Icon(Icons.add, color: Colors.white, size: 18),
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
                        icon: const Icon(Icons.delete, color: Colors.redAccent),
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
                  "👥 सभी इंस्टॉल और Google IDs:",
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
                        "Google ID: ${u['email']}\nपास: ${u['credits']} | मूवी बनाई: ${u['movies_created']} | पेड: ₹${u['total_paid']}",
                      ),
                      trailing: IconButton(
                        icon: const Icon(
                          Icons.add_circle,
                          color: Color(0xFFFFD600),
                        ),
                        tooltip: "+1 फ्री पास दें",
                        onPressed: () => _grantCredit(u['email']),
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
                fontSize: 22,
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
