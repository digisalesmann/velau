import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:local_auth/local_auth.dart';
import '../services/api_service.dart';
import 'home_shell.dart';

class AccountScreen extends StatefulWidget {
  const AccountScreen({super.key});
  @override
  State<AccountScreen> createState() => _AccountScreenState();
}

class _AccountScreenState extends State<AccountScreen> {
  final ApiService _api = ApiService();
  final LocalAuthentication _localAuth = LocalAuthentication();

  final Color _bg = const Color(0xFFFFFFFF);
  final Color _green = const Color(0xFF10B981);
  final Color _textDark = const Color(0xFF0F172A);
  final Color _textMuted = const Color(0xFF64748B);
  final Color _separator = const Color(0xFFF1F5F9);
  final Color _red = const Color(0xFFEF4444);
  final Color _amber = const Color(0xFFF59E0B);

  String _username = 'Trader';
  String _initials = 'TR';
  bool _loading = true;
  bool _isAdmin = false;
  bool _biometricsEnabled = false;
  bool _biometricsAvailable = false;
  bool _is2faEnabled = false;

  // Deriv connection state
  bool _derivConnected = false;
  String _derivAccountId = '';
  double _derivBalance = 0.0;
  String _derivCurrency = 'USD';
  bool _derivChecking = false;

  // Circuit breaker
  bool _circuitBroken = false;
  int _consecLosses = 0;

  @override
  void initState() {
    super.initState();
    _loadAccountData();
  }

  Future<void> _loadAccountData() async {
    setState(() => _loading = true);
    final prefs = await SharedPreferences.getInstance();
    _biometricsEnabled = prefs.getBool('biometrics_enabled') ?? false;
    _is2faEnabled = prefs.getBool('2fa_enabled') ?? false;
    _isAdmin = prefs.getBool('is_admin') ?? false;

    final canCheck = await _localAuth.canCheckBiometrics;
    final isSupported = await _localAuth.isDeviceSupported();
    _biometricsAvailable = canCheck && isSupported;

    try {
      final token = prefs.getString('jwt_token');
      if (token != null) {
        final data = await _api.getDashboardData(token);
        if (mounted) {
          setState(() {
            _username = data['username'] ?? 'Trader';
            _initials = _username.length >= 2
                ? _username.substring(0, 2).toUpperCase()
                : _username.toUpperCase();
            _circuitBroken = data['circuit_broken'] ?? false;
            _consecLosses = data['consecutive_losses'] ?? 0;
            _derivConnected = data['deriv_connected'] ?? false;
          });
        }

        // Sync 2FA status from backend — SharedPreferences is unreliable across
        // devices/reinstalls since it is local storage only.
        try {
          final tfaStatus = await _api.get2faStatus(token);
          final enabled = tfaStatus['enabled'] == true;
          await prefs.setBool('2fa_enabled', enabled);
          if (mounted) setState(() => _is2faEnabled = enabled);
        } catch (_) {
          // Non-fatal — keep whatever value is in SharedPreferences
        }

        // Check Deriv status separately
        await _checkDerivStatus(token);
      }
    } catch (e) {
      debugPrint('Account load: $e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _checkDerivStatus(String jwtToken) async {
    if (mounted) setState(() => _derivChecking = true);
    try {
      final data = await _api.getDerivStatus(jwtToken);
      if (mounted) {
        setState(() {
          _derivConnected = data['connected'] ?? false;
          _derivAccountId = data['account_id'] ?? '';
          _derivBalance = (data['balance'] ?? 0.0).toDouble();
          _derivCurrency = data['currency'] ?? 'USD';
        });
      }
    } catch (_) {
    } finally {
      if (mounted) setState(() => _derivChecking = false);
    }
  }

  TextStyle _satoshi({
    required Color color,
    required double fontSize,
    required FontWeight fontWeight,
    double letterSpacing = 0,
  }) => TextStyle(
    fontFamily: 'Satoshi',
    color: color,
    fontSize: fontSize,
    fontWeight: fontWeight,
    letterSpacing: letterSpacing,
  );

  void _snack(String msg, {Color? color}) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          msg,
          style: _satoshi(
            color: Colors.white,
            fontSize: 14,
            fontWeight: FontWeight.w600,
          ),
        ),
        backgroundColor: color ?? _textDark,
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      ),
    );
  }

  // ── Connect Deriv Modal ────────────────────────────────────────────────────
  void _showConnectDerivModal() {
    final tokenCtrl = TextEditingController();
    bool connecting = false;
    bool obscure = true;

    showModalBottomSheet(
      context: context,
      backgroundColor: _bg,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      builder: (ctx) => StatefulBuilder(
        builder: (ctx2, setModal) => Padding(
          padding: EdgeInsets.only(
            bottom: MediaQuery.of(ctx2).viewInsets.bottom,
            left: 24,
            right: 24,
            top: 24,
          ),
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // Handle
                Center(
                  child: Container(
                    width: 36,
                    height: 4,
                    margin: const EdgeInsets.only(bottom: 20),
                    decoration: BoxDecoration(
                      color: _separator,
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                ),

                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      'Connect Deriv Account',
                      style: _satoshi(
                        color: _textDark,
                        fontSize: 20,
                        fontWeight: FontWeight.w900,
                        letterSpacing: -0.5,
                      ),
                    ),
                    IconButton(
                      icon: Icon(Icons.close_rounded, color: _textDark),
                      onPressed: () => Navigator.pop(ctx),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  'Enter your Deriv API token to enable live trading on your account.',
                  style: _satoshi(
                    color: _textMuted,
                    fontSize: 13,
                    fontWeight: FontWeight.w500,
                  ),
                ),
                const SizedBox(height: 20),

                // How to get token info
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: _amber.withValues(alpha: 0.07),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: _amber.withValues(alpha: 0.2)),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(
                            Icons.info_outline_rounded,
                            color: _amber,
                            size: 15,
                          ),
                          const SizedBox(width: 8),
                          Text(
                            'How to get your API token',
                            style: _satoshi(
                              color: _amber,
                              fontSize: 12,
                              fontWeight: FontWeight.w800,
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 8),
                      Text(
                        '1. Go to legacy-api.deriv.com\n'
                        '2. Dashboard → API Token\n'
                        '3. Create token with: Admin, Read, Trade, Payments scopes\n'
                        '4. Use your Options account (VRTC for demo)',
                        style: _satoshi(
                          color: _amber,
                          fontSize: 12,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 20),

                // Token input
                TextFormField(
                  controller: tokenCtrl,
                  obscureText: obscure,
                  style: _satoshi(
                    color: _textDark,
                    fontSize: 14,
                    fontWeight: FontWeight.w600,
                  ),
                  decoration: InputDecoration(
                    labelText: 'Deriv API Token',
                    labelStyle: _satoshi(
                      color: _textMuted,
                      fontSize: 13,
                      fontWeight: FontWeight.w500,
                    ),
                    hintText: 'Paste your token here',
                    hintStyle: _satoshi(
                      color: _textMuted.withValues(alpha: 0.5),
                      fontSize: 13,
                      fontWeight: FontWeight.w400,
                    ),
                    filled: true,
                    fillColor: _separator,
                    suffixIcon: IconButton(
                      icon: Icon(
                        obscure
                            ? Icons.visibility_off_outlined
                            : Icons.visibility_outlined,
                        color: _textMuted,
                        size: 20,
                      ),
                      onPressed: () => setModal(() => obscure = !obscure),
                    ),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(12),
                      borderSide: BorderSide.none,
                    ),
                    contentPadding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 16,
                    ),
                  ),
                ),
                const SizedBox(height: 20),

                SizedBox(
                  width: double.infinity,
                  height: 54,
                  child: ElevatedButton(
                    onPressed: connecting
                        ? null
                        : () async {
                            final t = tokenCtrl.text.trim();
                            if (t.isEmpty) {
                              _snack(
                                'Please enter your API token.',
                                color: _red,
                              );
                              return;
                            }
                            setModal(() => connecting = true);
                            try {
                              final prefs =
                                  await SharedPreferences.getInstance();
                              final jwt = prefs.getString('jwt_token');
                              if (jwt == null) {
                                throw Exception('Session expired');
                              }

                              final result = await _api.connectDeriv(jwt, t);
                              if (mounted) {
                                Navigator.pop(ctx);
                                setState(() {
                                  _derivConnected = true;
                                  _derivAccountId = result['account_id'] ?? '';
                                  _derivBalance = (result['balance'] ?? 0.0)
                                      .toDouble();
                                  _derivCurrency = result['currency'] ?? 'USD';
                                });
                                HomeShell.derivNotifier.value++;
                                _snack(
                                  'Connected to ${result['account_id']}',
                                  color: _green,
                                );
                              }
                            } catch (e) {
                              setModal(() => connecting = false);
                              _snack(
                                e.toString().replaceAll('Exception: ', ''),
                                color: _red,
                              );
                            }
                          },
                    style: ElevatedButton.styleFrom(
                      backgroundColor: _green,
                      elevation: 0,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(100),
                      ),
                    ),
                    child: connecting
                        ? const SizedBox(
                            width: 22,
                            height: 22,
                            child: CircularProgressIndicator(
                              color: Colors.white,
                              strokeWidth: 2,
                            ),
                          )
                        : Text(
                            'Connect Account',
                            style: _satoshi(
                              color: Colors.white,
                              fontSize: 15,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                  ),
                ),
                const SizedBox(height: 36),
              ],
            ),
          ),
        ),
      ),
    );
  }

  // ── Disconnect confirmation ────────────────────────────────────────────────
  void _showDisconnectDialog() {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: _bg,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        title: Text(
          'Disconnect Deriv?',
          style: _satoshi(
            color: _textDark,
            fontSize: 18,
            fontWeight: FontWeight.w900,
          ),
        ),
        content: Text(
          'Your token will be removed. The bot will stop trading on your account.',
          style: _satoshi(
            color: _textMuted,
            fontSize: 14,
            fontWeight: FontWeight.w500,
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(
              'Cancel',
              style: _satoshi(
                color: _textDark,
                fontSize: 14,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          ElevatedButton(
            onPressed: () async {
              Navigator.pop(ctx);
              try {
                final prefs = await SharedPreferences.getInstance();
                final jwt = prefs.getString('jwt_token');
                if (jwt == null) return;
                await _api.disconnectDeriv(jwt);
                if (mounted) {
                  setState(() {
                    _derivConnected = false;
                    _derivAccountId = '';
                    _derivBalance = 0.0;
                  });
                  HomeShell.derivNotifier.value++;
                }
                _snack('Deriv account disconnected.');
              } catch (e) {
                _snack('Could not disconnect. Please try again.', color: _red);
              }
            },
            style: ElevatedButton.styleFrom(
              backgroundColor: _red,
              elevation: 0,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(100),
              ),
            ),
            child: Text(
              'Disconnect',
              style: _satoshi(
                color: Colors.white,
                fontSize: 14,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _toggleSecurity(String key, bool value) async {
    final prefs = await SharedPreferences.getInstance();
    if (key == 'biometrics_enabled') {
      if (value) {
        try {
          final ok = await _localAuth.authenticate(
            localizedReason: 'Confirm to enable biometric login',
            options: const AuthenticationOptions(biometricOnly: true),
          );
          if (!ok) {
            setState(() => _biometricsEnabled = false);
            await prefs.setBool('biometrics_enabled', false);
            _snack('Biometric setup cancelled.');
            return;
          }
          await prefs.setBool('biometrics_enabled', true);
          setState(() => _biometricsEnabled = true);
          _snack('Biometric login enabled.', color: _green);
        } on PlatformException catch (_) {
          setState(() => _biometricsEnabled = false);
          await prefs.setBool('biometrics_enabled', false);
          _snack(
            'Biometric authentication failed. Please try again.',
            color: _red,
          );
        }
        return;
      }
      await prefs.setBool('biometrics_enabled', false);
      setState(() => _biometricsEnabled = false);
      return;
    }
    // ── 2FA toggle ──────────────────────────────────────────────────────────
    if (key == '2fa_enabled') {
      if (value) {
        // Turning ON — revert immediately, navigate to setup; re-enable on success
        setState(() => _is2faEnabled = false);
        if (!mounted) return;
        final navigator = Navigator.of(context);
        final ok = await navigator.pushNamed('/2fa_setup');
        if (!mounted) return;
        if (ok == true) {
          await prefs.setBool('2fa_enabled', true);
          setState(() => _is2faEnabled = true);
          _snack('Two-factor authentication enabled.', color: _green);
        }
      } else {
        // Turning OFF — ask for TOTP code first
        _show2faDisableSheet();
      }
      return;
    }

    await prefs.setBool(key, value);
  }

  void _show2faDisableSheet() {
    final codeCtrl = TextEditingController();
    bool disabling = false;
    String? sheetError;

    showModalBottomSheet(
      context: context,
      backgroundColor: _bg,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      builder: (ctx) => StatefulBuilder(
        builder: (ctx2, setSheet) => Padding(
          padding: EdgeInsets.only(
            bottom: MediaQuery.of(ctx2).viewInsets.bottom + 24,
            left: 24,
            right: 24,
            top: 24,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  width: 36,
                  height: 4,
                  margin: const EdgeInsets.only(bottom: 20),
                  decoration: BoxDecoration(
                    color: _separator,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              Text(
                'Disable 2FA',
                style: _satoshi(
                  color: _textDark,
                  fontSize: 20,
                  fontWeight: FontWeight.w900,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'Enter the 6-digit code from your authenticator app to disable two-factor authentication.',
                style: _satoshi(
                  color: _textMuted,
                  fontSize: 14,
                  fontWeight: FontWeight.w500,
                ),
              ),
              const SizedBox(height: 20),
              TextFormField(
                controller: codeCtrl,
                keyboardType: TextInputType.number,
                maxLength: 6,
                inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                autofocus: true,
                style: _satoshi(
                  color: _textDark,
                  fontSize: 22,
                  fontWeight: FontWeight.w700,
                ).copyWith(letterSpacing: 6),
                textAlign: TextAlign.center,
                decoration: InputDecoration(
                  counterText: '',
                  hintText: '000000',
                  hintStyle: _satoshi(
                    color: _textMuted.withValues(alpha: 0.4),
                    fontSize: 22,
                    fontWeight: FontWeight.w500,
                  ).copyWith(letterSpacing: 6),
                  filled: true,
                  fillColor: const Color(0xFFF8FAFC),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(16),
                    borderSide: BorderSide.none,
                  ),
                  focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(16),
                    borderSide: BorderSide(
                      color: _green.withValues(alpha: 0.5),
                      width: 1.5,
                    ),
                  ),
                  contentPadding: const EdgeInsets.symmetric(
                    horizontal: 20,
                    vertical: 18,
                  ),
                ),
              ),
              if (sheetError != null) ...[
                const SizedBox(height: 8),
                Text(
                  sheetError!,
                  style: _satoshi(
                    color: _red,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
              const SizedBox(height: 20),
              SizedBox(
                width: double.infinity,
                height: 52,
                child: ElevatedButton(
                  onPressed: disabling
                      ? null
                      : () async {
                          final code = codeCtrl.text.trim();
                          if (code.length != 6) {
                            setSheet(
                              () => sheetError = 'Enter a 6-digit code.',
                            );
                            return;
                          }
                          setSheet(() {
                            disabling = true;
                            sheetError = null;
                          });
                          try {
                            final prefs = await SharedPreferences.getInstance();
                            final token = prefs.getString('jwt_token')!;
                            await _api.disable2fa(token, code);
                            await prefs.setBool('2fa_enabled', false);
                            if (!mounted) return;
                            setState(() => _is2faEnabled = false);
                            if (ctx2.mounted) Navigator.pop(ctx2);
                            _snack('Two-factor authentication disabled.');
                          } catch (e) {
                            setSheet(() {
                              sheetError = e.toString().replaceAll(
                                'Exception: ',
                                '',
                              );
                              disabling = false;
                            });
                          }
                        },
                  style: ElevatedButton.styleFrom(
                    backgroundColor: _red,
                    foregroundColor: Colors.white,
                    elevation: 0,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(100),
                    ),
                  ),
                  child: disabling
                      ? const CircularProgressIndicator(
                          color: Colors.white,
                          strokeWidth: 2,
                        )
                      : Text(
                          'Disable 2FA',
                          style: _satoshi(
                            color: Colors.white,
                            fontSize: 15,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  void _showLogoutDialog() {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: _bg,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        title: Text(
          'Log Out',
          style: _satoshi(
            color: _textDark,
            fontSize: 20,
            fontWeight: FontWeight.w900,
          ),
        ),
        content: Text(
          'Your trading bot continues running in the cloud.',
          style: _satoshi(
            color: _textMuted,
            fontSize: 14,
            fontWeight: FontWeight.w500,
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(
              'Cancel',
              style: _satoshi(
                color: _textDark,
                fontSize: 14,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          ElevatedButton(
            onPressed: () async {
              final prefs = await SharedPreferences.getInstance();
              await prefs.remove('jwt_token');
              if (ctx.mounted) {
                Navigator.pop(ctx);
              }
              if (mounted) {
                Navigator.pushReplacementNamed(context, '/login');
              }
            },
            style: ElevatedButton.styleFrom(
              backgroundColor: _red,
              elevation: 0,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(100),
              ),
            ),
            child: Text(
              'Log Out',
              style: _satoshi(
                color: Colors.white,
                fontSize: 14,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
        ],
      ),
    );
  }

  void _showChangePasswordModal() {
    final currentCtrl = TextEditingController();
    final newCtrl = TextEditingController();
    final confirmCtrl = TextEditingController();
    bool obscure = true;

    showModalBottomSheet(
      context: context,
      backgroundColor: _bg,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      builder: (ctx) => StatefulBuilder(
        builder: (ctx2, setModal) => Padding(
          padding: EdgeInsets.only(
            bottom: MediaQuery.of(ctx2).viewInsets.bottom,
            left: 24,
            right: 24,
            top: 24,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  width: 36,
                  height: 4,
                  margin: const EdgeInsets.only(bottom: 20),
                  decoration: BoxDecoration(
                    color: _separator,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text(
                    'Change Password',
                    style: _satoshi(
                      color: _textDark,
                      fontSize: 20,
                      fontWeight: FontWeight.w900,
                    ),
                  ),
                  IconButton(
                    icon: Icon(Icons.close_rounded, color: _textDark),
                    onPressed: () => Navigator.pop(ctx),
                  ),
                ],
              ),
              const SizedBox(height: 20),
              _pwField(
                'Current Password',
                currentCtrl,
                obscure,
                () => setModal(() => obscure = !obscure),
              ),
              const SizedBox(height: 14),
              _pwField('New Password', newCtrl, obscure, null),
              const SizedBox(height: 14),
              _pwField('Confirm New Password', confirmCtrl, obscure, null),
              const SizedBox(height: 28),
              SizedBox(
                width: double.infinity,
                height: 54,
                child: ElevatedButton(
                  onPressed: () {
                    if (newCtrl.text != confirmCtrl.text) {
                      _snack('Passwords do not match.', color: _red);
                      return;
                    }
                    if (newCtrl.text.length < 8) {
                      _snack('Minimum 8 characters.', color: _red);
                      return;
                    }
                    Navigator.pop(ctx);
                    _snack('Password updated.', color: _green);
                  },
                  style: ElevatedButton.styleFrom(
                    backgroundColor: _textDark,
                    elevation: 0,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(100),
                    ),
                  ),
                  child: Text(
                    'Update Password',
                    style: _satoshi(
                      color: Colors.white,
                      fontSize: 15,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 36),
            ],
          ),
        ),
      ),
    );
  }

  Widget _pwField(
    String label,
    TextEditingController ctrl,
    bool obscure,
    VoidCallback? onToggle,
  ) => TextFormField(
    controller: ctrl,
    obscureText: obscure,
    style: _satoshi(
      color: _textDark,
      fontSize: 15,
      fontWeight: FontWeight.w600,
    ),
    decoration: InputDecoration(
      labelText: label,
      labelStyle: _satoshi(
        color: _textMuted,
        fontSize: 13,
        fontWeight: FontWeight.w500,
      ),
      filled: true,
      fillColor: _separator,
      suffixIcon: onToggle != null
          ? IconButton(
              icon: Icon(
                obscure
                    ? Icons.visibility_off_outlined
                    : Icons.visibility_outlined,
                color: _textMuted,
                size: 20,
              ),
              onPressed: onToggle,
            )
          : null,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: BorderSide.none,
      ),
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
    ),
  );

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bg,
      appBar: AppBar(
        backgroundColor: _bg,
        elevation: 0,
        scrolledUnderElevation: 0,
        titleSpacing: 24,
        title: Text(
          'Account',
          style: _satoshi(
            color: _textDark,
            fontSize: 24,
            fontWeight: FontWeight.w900,
            letterSpacing: -0.5,
          ),
        ),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(1),
          child: Divider(color: _separator, height: 1, thickness: 1),
        ),
      ),
      body: _loading
          ? Center(
              child: CircularProgressIndicator(color: _green, strokeWidth: 2),
            )
          : SafeArea(
              child: ListView(
                padding: const EdgeInsets.only(bottom: 40),
                children: [
                  // ── Profile ─────────────────────────────────────────────────
                  Padding(
                    padding: const EdgeInsets.fromLTRB(24, 28, 24, 0),
                    child: Row(
                      children: [
                        Container(
                          height: 56,
                          width: 56,
                          decoration: BoxDecoration(
                            color: _green.withValues(alpha: 0.08),
                            shape: BoxShape.circle,
                          ),
                          alignment: Alignment.center,
                          child: Text(
                            _initials,
                            style: _satoshi(
                              color: _green,
                              fontSize: 20,
                              fontWeight: FontWeight.w900,
                            ),
                          ),
                        ),
                        const SizedBox(width: 16),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                _username,
                                overflow: TextOverflow.ellipsis,
                                style: _satoshi(
                                  color: _textDark,
                                  fontSize: 17,
                                  fontWeight: FontWeight.w900,
                                  letterSpacing: -0.4,
                                ),
                              ),
                              const SizedBox(height: 3),
                              Text(
                                'Algorithmic Trader',
                                style: _satoshi(
                                  color: _green,
                                  fontSize: 13,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 36),
                  Divider(color: _separator, height: 1, thickness: 1),

                  // ── Deriv Connection ─────────────────────────────────────────
                  _sectionLabel('DERIV ACCOUNT'),
                  _buildDerivRow(),
                  Divider(color: _separator, height: 1, thickness: 1),

                  // ── Security ─────────────────────────────────────────────────
                  const SizedBox(height: 8),
                  Divider(color: _separator, height: 1, thickness: 1),
                  _sectionLabel('SECURITY'),
                  _toggleRow(
                    Icons.verified_user_rounded,
                    'Two-Factor Auth',
                    '2FA',
                    _is2faEnabled,
                    (v) => _toggleSecurity('2fa_enabled', v),
                  ),
                  Divider(
                    color: _separator,
                    height: 1,
                    thickness: 1,
                    indent: 24,
                    endIndent: 24,
                  ),
                  if (_biometricsAvailable) ...[
                    _toggleRow(
                      Icons.fingerprint_rounded,
                      'Biometric Login',
                      'Face ID / Fingerprint',
                      _biometricsEnabled,
                      (v) => _toggleSecurity('biometrics_enabled', v),
                    ),
                    Divider(
                      color: _separator,
                      height: 1,
                      thickness: 1,
                      indent: 24,
                      endIndent: 24,
                    ),
                  ],
                  _actionRow(
                    Icons.vpn_key_outlined,
                    'Change Password',
                    onTap: _showChangePasswordModal,
                  ),

                  const SizedBox(height: 8),
                  Divider(color: _separator, height: 1, thickness: 1),

                  // ── System ───────────────────────────────────────────────────
                  _sectionLabel('SYSTEM'),
                  if (_isAdmin) ...[
                    _actionRow(
                      Icons.manage_accounts_rounded,
                      'Admin Panel',
                      onTap: () => Navigator.pushNamed(context, '/admin'),
                    ),
                    Divider(
                      color: _separator,
                      height: 1,
                      thickness: 1,
                      indent: 24,
                      endIndent: 24,
                    ),
                  ],
                  _actionRow(
                    Icons.settings_outlined,
                    'App Settings',
                    onTap: () => Navigator.pushNamed(context, '/settings'),
                  ),
                  Divider(
                    color: _separator,
                    height: 1,
                    thickness: 1,
                    indent: 24,
                    endIndent: 24,
                  ),
                  _actionRow(
                    Icons.logout_rounded,
                    'Log Out',
                    titleColor: _red,
                    hideArrow: true,
                    onTap: _showLogoutDialog,
                  ),
                ],
              ),
            ),
    );
  }

  Widget _buildDerivRow() {
    if (_derivChecking) {
      return Padding(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 18),
        child: Row(
          children: [
            Icon(Icons.cable_rounded, color: _textDark, size: 20),
            const SizedBox(width: 14),
            Text(
              'Checking connection...',
              style: _satoshi(
                color: _textMuted,
                fontSize: 15,
                fontWeight: FontWeight.w600,
              ),
            ),
            const Spacer(),
            SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(color: _green, strokeWidth: 2),
            ),
          ],
        ),
      );
    }

    if (_derivConnected) {
      return Column(
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
            child: Row(
              children: [
                Icon(Icons.cable_rounded, color: _textDark, size: 20),
                const SizedBox(width: 14),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Container(
                            width: 7,
                            height: 7,
                            decoration: BoxDecoration(
                              color: _green,
                              shape: BoxShape.circle,
                            ),
                          ),
                          const SizedBox(width: 6),
                          Text(
                            'CONNECTED',
                            style: _satoshi(
                              color: _green,
                              fontSize: 11,
                              fontWeight: FontWeight.w900,
                              letterSpacing: 0.5,
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 3),
                      Text(
                        '$_derivAccountId  •  $_derivCurrency '
                        '${_derivBalance.toStringAsFixed(2)}',
                        style: _satoshi(
                          color: _textMuted,
                          fontSize: 13,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                      const SizedBox(height: 10),
                      GestureDetector(
                        onTap: _showDisconnectDialog,
                        child: Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 12,
                            vertical: 6,
                          ),
                          decoration: BoxDecoration(
                            color: _red.withValues(alpha: 0.08),
                            borderRadius: BorderRadius.circular(8),
                            border: Border.all(
                              color: _red.withValues(alpha: 0.2),
                            ),
                          ),
                          child: Text(
                            'Disconnect',
                            style: _satoshi(
                              color: _red,
                              fontSize: 12,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          if (_circuitBroken)
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 0, 24, 12),
              child: Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: _red.withValues(alpha: 0.06),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: _red.withValues(alpha: 0.2)),
                ),
                child: Row(
                  children: [
                    Icon(Icons.warning_amber_rounded, color: _red, size: 15),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        'Circuit breaker: $_consecLosses losses. Resets midnight UTC.',
                        style: _satoshi(
                          color: _red,
                          fontSize: 12,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
        ],
      );
    }

    // Not connected
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
      child: Row(
        children: [
          Icon(Icons.cable_rounded, color: _textMuted, size: 20),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Deriv API Connection',
                  style: _satoshi(
                    color: _textDark,
                    fontSize: 15,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                Text(
                  'Not connected, tap to connect your account',
                  style: _satoshi(
                    color: _textMuted,
                    fontSize: 12,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ],
            ),
          ),
          GestureDetector(
            onTap: _showConnectDerivModal,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
              decoration: BoxDecoration(
                color: _green,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                'Connect',
                style: _satoshi(
                  color: Colors.white,
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _sectionLabel(String label) => Padding(
    padding: const EdgeInsets.fromLTRB(24, 20, 24, 4),
    child: Text(
      label,
      style: _satoshi(
        color: _textMuted,
        fontSize: 10,
        fontWeight: FontWeight.w700,
        letterSpacing: 0.8,
      ),
    ),
  );

  Widget _actionRow(
    IconData icon,
    String title, {
    Color? titleColor,
    Widget? trailing,
    bool hideArrow = false,
    VoidCallback? onTap,
  }) => InkWell(
    onTap: onTap ?? () {},
    splashColor: _separator,
    child: Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
      child: Row(
        children: [
          Icon(icon, color: titleColor ?? _textDark, size: 20),
          const SizedBox(width: 14),
          Expanded(
            child: Text(
              title,
              style: _satoshi(
                color: titleColor ?? _textDark,
                fontSize: 15,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          ?trailing,
          if (!hideArrow) ...[
            const SizedBox(width: 8),
            Icon(
              Icons.arrow_forward_ios_rounded,
              color: _textMuted.withValues(alpha: 0.4),
              size: 13,
            ),
          ],
        ],
      ),
    ),
  );

  Widget _toggleRow(
    IconData icon,
    String title,
    String subtitle,
    bool value,
    ValueChanged<bool> onChanged,
  ) => Padding(
    padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 10),
    child: Row(
      children: [
        Icon(icon, color: _textDark, size: 20),
        const SizedBox(width: 14),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: _satoshi(
                  color: _textDark,
                  fontSize: 15,
                  fontWeight: FontWeight.w700,
                ),
              ),
              Text(
                subtitle,
                style: _satoshi(
                  color: _textMuted,
                  fontSize: 12,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ],
          ),
        ),
        Switch.adaptive(
          value: value,
          onChanged: onChanged,
          activeThumbColor: _green,
          activeTrackColor: _green.withValues(alpha: 0.4),
        ),
      ],
    ),
  );
}
