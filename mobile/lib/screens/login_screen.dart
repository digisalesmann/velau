import 'package:flutter/material.dart';
import 'package:firebase_auth/firebase_auth.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final FirebaseAuth _firebaseAuth = FirebaseAuth.instance;
  final ApiService _apiService = ApiService();
  bool _isLoading = false;
  final _emailController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _obscurePassword = true;

  // Core White & Green Fintech Palette
  final Color _bgColor = const Color(0xFFFFFFFF);
  final Color _primaryGreen = const Color(0xFF10B981);
  final Color _textDark = const Color(0xFF0F172A);
  final Color _textMuted = const Color(0xFF64748B);
  final Color _inputFillColor = const Color(0xFFF8FAFC);

  TextStyle _satoshi({
    required Color color,
    required double fontSize,
    required FontWeight fontWeight,
    double letterSpacing = 0,
  }) {
    return TextStyle(
      fontFamily: 'Satoshi',
      color: color,
      fontSize: fontSize,
      fontWeight: fontWeight,
      letterSpacing: letterSpacing,
    );
  }

  @override
  void dispose() {
    _emailController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  void _handleLogin() async {
    final email = _emailController.text.trim();
    final password = _passwordController.text;

    if (email.isEmpty || password.isEmpty) return;

    setState(() => _isLoading = true);
    try {
      // Step 1: Firebase login
      final userCredential = await _firebaseAuth.signInWithEmailAndPassword(
        email: email,
        password: password,
      );

      // Step 2: Exchange Firebase ID token for a backend JWT.
      // This always works regardless of password sync state.
      final idToken = await userCredential.user!.getIdToken();
      final backendResponse = await _apiService.loginWithFirebase(idToken!);
      final String token   = backendResponse['access_token'] as String;
      final bool isAdmin   = backendResponse['is_admin'] == true;

      // Step 3: Save JWT + admin flag
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('jwt_token', token);
      await prefs.setBool('is_admin', isAdmin);

      if (!mounted) return;
      setState(() => _isLoading = false);

      // Step 4: Check 2FA — gate access behind verify screen if enabled
      try {
        final tfaStatus = await _apiService.get2faStatus(token);
        if (tfaStatus['enabled'] == true) {
          final destination = await _resolveDestination(token);
          if (!mounted) return;
          Navigator.pushReplacementNamed(
            context,
            '/2fa_verify',
            arguments: {'destination': destination},
          );
          return;
        }
      } catch (_) {
        // 2FA check failed — proceed without gate
      }

      // Step 5: Check subscription — gate access
      final destination = await _resolveDestination(token);
      if (!mounted) return;
      Navigator.pushReplacementNamed(context, destination);
    } on FirebaseAuthException catch (e) {
      if (!mounted) return;
      setState(() => _isLoading = false);
      _showError(e.message ?? 'Login error');
    } catch (e) {
      if (!mounted) return;
      setState(() => _isLoading = false);
      _showError(ApiService.friendlyError(e));
    }
  }

  /// Returns '/home' if the user has an active subscription, '/subscription' otherwise.
  /// Also refreshes the is_admin flag in SharedPreferences.
  /// Fails open (returns '/home') on network errors so a bad connection doesn't lock users out.
  Future<String> _resolveDestination(String token) async {
    try {
      final status = await _apiService.getSubscriptionStatus(token);
      // Persist fresh is_admin so account screen shows Admin button immediately
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('is_admin', status['is_admin'] == true);
      if (status['active'] == true) return '/home';
      return '/subscription';
    } catch (_) {
      return '/home'; // fail open
    }
  }

  void _showError(String message) {
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bgColor,
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.symmetric(horizontal: 24.0, vertical: 40.0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const SizedBox(height: 52),

              // --- Header ---
              Text(
                'Welcome Back',
                style: _satoshi(
                  color: _textDark,
                  fontSize: 32,
                  fontWeight: FontWeight.w900,
                  letterSpacing: -1.0,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'Sign in to your trading account.',
                style: _satoshi(
                  color: _textMuted,
                  fontSize: 16,
                  fontWeight: FontWeight.w500,
                ),
              ),
              const SizedBox(height: 48),

              // --- Form Fields ---
              _buildInputLabel('Email Address'),
              _buildTextField(
                controller: _emailController,
                hintText: 'name@example.com',
                icon: Icons.email_outlined,
                keyboardType: TextInputType.emailAddress,
              ),
              const SizedBox(height: 24),

              _buildInputLabel('Password'),
              _buildTextField(
                controller: _passwordController,
                hintText: 'Enter your password',
                icon: Icons.lock_outline_rounded,
                isPassword: true,
                obscureText: _obscurePassword,
                onToggleVisibility: () {
                  setState(() => _obscurePassword = !_obscurePassword);
                },
              ),
              const SizedBox(height: 16),

              // --- Forgot Password ---
              Align(
                alignment: Alignment.centerRight,
                child: TextButton(
                  onPressed: () =>
                      Navigator.pushNamed(context, '/forgot_password'),
                  child: Text(
                    'Forgot Password?',
                    style: _satoshi(
                      color: _primaryGreen,
                      fontSize: 14,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 24),

              // --- Login Button ---
              SizedBox(
                width: double.infinity,
                height: 56,
                child: ElevatedButton(
                  onPressed: _isLoading ? null : _handleLogin,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: _primaryGreen,
                    foregroundColor: Colors.white,
                    elevation: 0,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(100),
                    ),
                  ),
                  child: _isLoading
                      ? const CircularProgressIndicator(color: Colors.white)
                      : Text(
                          'Sign In',
                          style: _satoshi(
                            color: Colors.white,
                            fontSize: 16,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                ),
              ),
              const SizedBox(height: 48),

              // --- Register Link ---
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Text(
                    "Don't have an account? ",
                    style: _satoshi(
                      color: _textMuted,
                      fontSize: 15,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  GestureDetector(
                    onTap: () => Navigator.pushNamed(context, '/register'),
                    child: Text(
                      'Create one',
                      style: _satoshi(
                        color: _textDark,
                        fontSize: 15,
                        fontWeight: FontWeight.w900,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 24),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildInputLabel(String label) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8.0, left: 4.0),
      child: Text(
        label,
        style: _satoshi(
          color: _textDark,
          fontSize: 14,
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }

  Widget _buildTextField({
    required TextEditingController controller,
    required String hintText,
    required IconData icon,
    bool isPassword = false,
    bool obscureText = false,
    VoidCallback? onToggleVisibility,
    TextInputType keyboardType = TextInputType.text,
  }) {
    return TextFormField(
      controller: controller,
      obscureText: obscureText,
      keyboardType: keyboardType,
      style: _satoshi(
        color: _textDark,
        fontSize: 16,
        fontWeight: FontWeight.w700,
      ),
      decoration: InputDecoration(
        hintText: hintText,
        hintStyle: _satoshi(
          color: _textMuted.withValues(alpha: 0.5),
          fontSize: 16,
          fontWeight: FontWeight.w500,
        ),
        prefixIcon: Icon(icon, color: _textMuted, size: 22),
        suffixIcon: isPassword
            ? IconButton(
                icon: Icon(
                  obscureText
                      ? Icons.visibility_off_outlined
                      : Icons.visibility_outlined,
                  color: _textMuted,
                  size: 22,
                ),
                onPressed: onToggleVisibility,
              )
            : null,
        filled: true,
        fillColor: _inputFillColor,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(16),
          borderSide: BorderSide.none,
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(16),
          borderSide: BorderSide(
            color: _primaryGreen.withValues(alpha: 0.5),
            width: 1.5,
          ),
        ),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 20,
          vertical: 18,
        ),
      ),
    );
  }
}
