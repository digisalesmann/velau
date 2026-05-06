import 'dart:async';
import 'dart:math' show sin, pi;
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';
import 'home_shell.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen>
    with TickerProviderStateMixin {
  final ApiService _apiService = ApiService();
  Map<String, dynamic>? _dashboardData;
  List<dynamic> _openContracts = [];
  bool _loading = true;
  String? _error;
  Timer? _contractTimer;
  late AnimationController _scanController;

  final Color _bgColor = const Color(0xFFFFFFFF);
  final Color _primaryGreen = const Color(0xFF10B981);
  final Color _textDark = const Color(0xFF0F172A);
  final Color _textMuted = const Color(0xFF64748B);
  final Color _dividerColor = const Color(0xFFF1F5F9);
  final Color _dangerRed = const Color(0xFFEF4444);
  final Color _amber = const Color(0xFFF59E0B);

  @override
  void initState() {
    super.initState();
    _scanController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2400),
    )..repeat();
    _fetchDashboard();
    HomeShell.derivNotifier.addListener(_onDerivChange);
    _contractTimer = Timer.periodic(const Duration(seconds: 10), (_) {
      if (_dashboardData?['trade_in_progress'] == true) {
        _fetchOpenContracts();
      }
    });
  }

  void _onDerivChange() => _fetchDashboard();

  /// Wraps a non-scrollable widget so RefreshIndicator's pull gesture works.
  Widget _scrollable(Widget child) => SingleChildScrollView(
    physics: const AlwaysScrollableScrollPhysics(),
    child: SizedBox(
      height: MediaQuery.of(context).size.height * 0.8,
      child: child,
    ),
  );

  @override
  void dispose() {
    HomeShell.derivNotifier.removeListener(_onDerivChange);
    _contractTimer?.cancel();
    _scanController.dispose();
    super.dispose();
  }

  Future<void> _fetchDashboard() async {
    if (mounted) setState(() => _loading = true);
    try {
      final prefs = await SharedPreferences.getInstance();
      final token = prefs.getString('jwt_token');
      if (token == null) throw Exception('Authentication missing.');
      final data = await _apiService.getDashboardData(token);
      if (mounted) {
        setState(() {
          _dashboardData = data;
          _loading = false;
          _error = null;
        });
      }
      // Fetch open contracts in parallel if trade in progress
      if (data['trade_in_progress'] == true) {
        _fetchOpenContracts();
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = ApiService.friendlyError(e);
          _loading = false;
        });
      }
    }
  }

  Future<void> _fetchOpenContracts() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final token = prefs.getString('jwt_token');
      if (token == null) return;
      final data = await _apiService.getOpenContracts(token);
      if (mounted) {
        setState(() => _openContracts = data['contracts'] ?? []);
      }
    } catch (_) {}
  }

  /// Strips the email domain so "john@gmail.com" shows as "john".
  String _displayName(dynamic raw) {
    final s = (raw ?? 'Trader').toString();
    final atIdx = s.indexOf('@');
    final name = atIdx > 0 ? s.substring(0, atIdx) : s;
    return name[0].toUpperCase() + name.substring(1);
  }

  String _getGreeting() {
    final h = DateTime.now().hour;
    if (h < 12) return 'Good morning,';
    if (h < 17) return 'Good afternoon,';
    return 'Good evening,';
  }

  Color _getBiasColor(String bias) {
    final b = bias.toUpperCase();
    if (b.contains('BULL')) return _primaryGreen;
    if (b.contains('BEAR')) return _dangerRed;
    return _textMuted;
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

  String _formatCurrency(dynamic amount) {
    final v = double.tryParse(amount?.toString() ?? '0') ?? 0.0;
    return NumberFormat('#,##0.00', 'en_US').format(v);
  }

  @override
  Widget build(BuildContext context) {
    final hPadding = MediaQuery.of(context).size.width > 600 ? 40.0 : 24.0;
    return Scaffold(
      backgroundColor: _bgColor,
      body: SafeArea(
        child: RefreshIndicator(
          color: _primaryGreen,
          onRefresh: _fetchDashboard,
          child: _loading
              ? Center(child: CircularProgressIndicator(color: _primaryGreen))
              : _error == 'no_deriv_account'
              ? _scrollable(_buildConnectPrompt())
              : _error != null
              ? _scrollable(_buildErrorState())
              : _buildContent(hPadding),
        ),
      ),
    );
  }

  Widget _buildErrorState() => Center(
    child: Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(Icons.wifi_off_rounded, color: _dangerRed, size: 48),
          const SizedBox(height: 16),
          Text(
            'Sync Failed',
            style: _satoshi(
              color: _textDark,
              fontSize: 20,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            _error ?? 'Unable to connect.',
            textAlign: TextAlign.center,
            style: _satoshi(
              color: _textMuted,
              fontSize: 14,
              fontWeight: FontWeight.w500,
            ),
          ),
          const SizedBox(height: 24),
          ElevatedButton(
            onPressed: _fetchDashboard,
            style: ElevatedButton.styleFrom(
              backgroundColor: _textDark,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(100),
              ),
            ),
            child: Text(
              'Retry Sync',
              style: _satoshi(
                color: Colors.white,
                fontSize: 14,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
        ],
      ),
    ),
  );

  Widget _buildConnectPrompt() => Center(
    child: Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Container(
            width: 72,
            height: 72,
            decoration: BoxDecoration(
              color: _primaryGreen.withValues(alpha: 0.1),
              shape: BoxShape.circle,
            ),
            child: Icon(Icons.link_rounded, color: _primaryGreen, size: 36),
          ),
          const SizedBox(height: 20),
          Text(
            'Connect Your Account',
            style: _satoshi(
              color: _textDark,
              fontSize: 20,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            'Link your Deriv account to start\ntrading and view your dashboard.',
            textAlign: TextAlign.center,
            style: _satoshi(
              color: _textMuted,
              fontSize: 14,
              fontWeight: FontWeight.w500,
            ),
          ),
          const SizedBox(height: 28),
          ElevatedButton(
            onPressed: () => HomeShell.tabNotifier.value = 3,
            style: ElevatedButton.styleFrom(
              backgroundColor: _primaryGreen,
              elevation: 0,
              padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 14),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(100),
              ),
            ),
            child: Text(
              'Go to Account',
              style: _satoshi(
                color: Colors.white,
                fontSize: 14,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
        ],
      ),
    ),
  );

  Widget _buildContent(double hPadding) {
    final currency = _dashboardData?['currency'] ?? 'USD';
    final balance = _formatCurrency(_dashboardData?['balance']);
    final marketBias = (_dashboardData?['market_bias'] ?? 'ANALYZING')
        .toString()
        .toUpperCase();

    final bool circuitBroken = _dashboardData?['circuit_broken'] ?? false;
    final int consecutiveLosses = _dashboardData?['consecutive_losses'] ?? 0;
    final bool tradeInProgress = _dashboardData?['trade_in_progress'] ?? false;
    final bool inSession = _dashboardData?['in_session'] ?? true;
    final String botStatus = _dashboardData?['bot_status'] ?? 'paused';
    final double winRate = (_dashboardData?['win_rate'] ?? 0.0).toDouble();
    final int tradesToday = _dashboardData?['trades_today'] ?? 0;
    final int totalTrades = _dashboardData?['total_trades'] ?? 0;
    final double dailyPnl = (_dashboardData?['daily_pnl'] ?? 0.0).toDouble();

    return SingleChildScrollView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: EdgeInsets.symmetric(horizontal: hPadding),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const SizedBox(height: 24),

          // ── Header ────────────────────────────────────────────────────────────
          Row(
            children: [
              Flexible(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      _getGreeting(),
                      style: _satoshi(
                        color: _textMuted,
                        fontSize: 14,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    Text(
                      _displayName(_dashboardData?['username']),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: _satoshi(
                        color: _textDark,
                        fontSize: 22,
                        fontWeight: FontWeight.w900,
                        letterSpacing: -0.5,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 32),

          // ── Safety banner ─────────────────────────────────────────────────────
          if (circuitBroken || !inSession || tradeInProgress)
            _buildSafetyBanner(
              circuitBroken,
              inSession,
              tradeInProgress,
              consecutiveLosses,
            ),

          // ── Bot status ────────────────────────────────────────────────────────
          _buildBotStatusStrip(
            botStatus,
            circuitBroken,
            inSession,
            tradeInProgress,
          ),
          const SizedBox(height: 32),

          // ── Balance ───────────────────────────────────────────────────────────
          Text(
            'Total Bot Equity',
            style: _satoshi(
              color: _textMuted,
              fontSize: 14,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 4),
          FittedBox(
            fit: BoxFit.scaleDown,
            child: Text(
              '$currency $balance',
              style: _satoshi(
                color: _textDark,
                fontSize: 36,
                fontWeight: FontWeight.w900,
                letterSpacing: -1.0,
              ),
            ),
          ),

          // Daily P&L under balance
          if (dailyPnl != 0) ...[
            const SizedBox(height: 6),
            Row(
              children: [
                Icon(
                  dailyPnl >= 0
                      ? Icons.arrow_upward_rounded
                      : Icons.arrow_downward_rounded,
                  color: dailyPnl >= 0 ? _primaryGreen : _dangerRed,
                  size: 14,
                ),
                const SizedBox(width: 4),
                Text(
                  '${dailyPnl >= 0 ? '+' : ''}\$${_formatCurrency(dailyPnl)} today',
                  style: _satoshi(
                    color: dailyPnl >= 0 ? _primaryGreen : _dangerRed,
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ],
            ),
          ],
          const SizedBox(height: 32),

          // ── Stats row ─────────────────────────────────────────────────────────
          Row(
            children: [
              _buildStat(
                'Win Rate',
                '${winRate.toStringAsFixed(1)}%',
                _textDark,
              ),
              _vDivider(),
              _buildStat('Today', '$tradesToday', _textDark),
              _vDivider(),
              _buildStat('Total', '$totalTrades', _textDark),
              _vDivider(),
              _buildStat('AI Bias', marketBias, _getBiasColor(marketBias)),
            ],
          ),
          const SizedBox(height: 16),

          if (consecutiveLosses > 0) _buildLossStreak(consecutiveLosses),
          const SizedBox(height: 32),

          // ── Quick actions ─────────────────────────────────────────────────────
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              _buildAction(
                Icons.account_balance_wallet_outlined,
                'Funding',
                () => Navigator.pushNamed(context, '/funding'),
                true,
              ),
              _buildAction(
                Icons.candlestick_chart_outlined,
                'Chart',
                () => Navigator.pushNamed(context, '/chart'),
                false,
              ),
              _buildAction(
                Icons.article_outlined,
                'News',
                () => Navigator.pushNamed(context, '/news'),
                false,
              ),
              _buildAction(
                Icons.settings_outlined,
                'Settings',
                () => Navigator.pushNamed(context, '/settings'),
                false,
              ),
            ],
          ),
          const SizedBox(height: 48),

          // ── Live trades ───────────────────────────────────────────────────────
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                'Live Trades',
                style: _satoshi(
                  color: _textDark,
                  fontSize: 20,
                  fontWeight: FontWeight.w900,
                  letterSpacing: -0.5,
                ),
              ),
              Text(
                'XAU/USD',
                style: _satoshi(
                  color: _primaryGreen,
                  fontSize: 13,
                  fontWeight: FontWeight.w900,
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),

          _openContracts.isNotEmpty
              ? Column(
                  children: _openContracts
                      .map((c) => _buildLiveContract(c))
                      .toList(),
                )
              : _buildEmptyTrades(tradeInProgress, marketBias),

          const SizedBox(height: 40),
        ],
      ),
    );
  }

  // ── Live contract card ────────────────────────────────────────────────────
  Widget _buildLiveContract(dynamic contract) {
    final double profit =
        double.tryParse(contract['profit']?.toString() ?? '0') ?? 0.0;
    final double stake =
        double.tryParse(contract['buy_price']?.toString() ?? '10') ?? 10.0;
    final String ctype = contract['contract_type']?.toString() ?? '';
    final bool isCall = ctype.toUpperCase().contains('CALL');
    final Color color = isCall ? _primaryGreen : _dangerRed;
    final Color pnlColor = profit >= 0 ? _primaryGreen : _dangerRed;
    final String symbol = contract['symbol'] ?? 'Volatility 100 (1s)';

    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.04),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: color.withValues(alpha: 0.15)),
      ),
      child: Row(
        children: [
          // Direction indicator
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(
              color: color.withValues(alpha: 0.1),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Icon(
              isCall ? Icons.trending_up_rounded : Icons.trending_down_rounded,
              color: color,
              size: 22,
            ),
          ),
          const SizedBox(width: 14),

          // Details
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Text(
                      isCall ? 'CALL' : 'PUT',
                      style: _satoshi(
                        color: color,
                        fontSize: 13,
                        fontWeight: FontWeight.w900,
                        letterSpacing: 0.3,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Flexible(
                      child: Text(
                        symbol,
                        overflow: TextOverflow.ellipsis,
                        style: _satoshi(
                          color: _textMuted,
                          fontSize: 12,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 4),
                Text(
                  'Stake \$${_formatCurrency(stake)}  •  15 min',
                  style: _satoshi(
                    color: _textMuted,
                    fontSize: 12,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ],
            ),
          ),

          // Live P&L
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(
                '${profit >= 0 ? '+' : ''}\$${_formatCurrency(profit)}',
                style: _satoshi(
                  color: pnlColor,
                  fontSize: 15,
                  fontWeight: FontWeight.w900,
                ),
              ),
              Row(
                children: [
                  Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(
                      color: _primaryGreen,
                      shape: BoxShape.circle,
                    ),
                  ),
                  const SizedBox(width: 4),
                  Text(
                    'LIVE',
                    style: _satoshi(
                      color: _primaryGreen,
                      fontSize: 9,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 0.6,
                    ),
                  ),
                ],
              ),
            ],
          ),
        ],
      ),
    );
  }

  // ── Safety banner ─────────────────────────────────────────────────────────
  Widget _buildSafetyBanner(
    bool circuitBroken,
    bool inSession,
    bool tradeInProgress,
    int losses,
  ) {
    String message;
    Color color;
    IconData icon;
    if (circuitBroken) {
      message =
          '$losses consecutive losses, bot paused for today. Resets at midnight UTC.';
      color = _dangerRed;
      icon = Icons.warning_amber_rounded;
    } else if (!inSession) {
      message =
          'Outside trading session, bot resumes at London open (07:00 UTC).';
      color = _amber;
      icon = Icons.bedtime_outlined;
    } else {
      message = 'Trade in progress, position is open.';
      color = _primaryGreen;
      icon = Icons.lock_clock_outlined;
    }
    return Container(
      margin: const EdgeInsets.only(bottom: 20),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.07),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: color.withValues(alpha: 0.2)),
      ),
      child: Row(
        children: [
          Icon(icon, color: color, size: 18),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              message,
              style: _satoshi(
                color: color,
                fontSize: 13,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildBotStatusStrip(
    String botStatus,
    bool circuitBroken,
    bool inSession,
    bool tradeInProgress,
  ) {
    String label;
    Color color;
    if (circuitBroken) {
      label = 'CIRCUIT BREAKER';
      color = _dangerRed;
    } else if (botStatus == 'paused') {
      label = 'BOT PAUSED';
      color = _textMuted;
    } else if (!inSession) {
      label = 'WAITING FOR SESSION';
      color = _amber;
    } else if (tradeInProgress) {
      label = 'TRADE OPEN';
      color = _primaryGreen;
    } else {
      label = 'SCANNING';
      color = _primaryGreen;
    }

    return Row(
      children: [
        Container(
          width: 7,
          height: 7,
          decoration: BoxDecoration(color: color, shape: BoxShape.circle),
        ),
        const SizedBox(width: 8),
        Text(
          label,
          style: _satoshi(
            color: color,
            fontSize: 12,
            fontWeight: FontWeight.w800,
            letterSpacing: 0.5,
          ),
        ),
      ],
    );
  }

  Widget _buildLossStreak(int losses) {
    final Color color = losses >= 3 ? _dangerRed : _amber;
    return Row(
      children: [
        ...List.generate(
          3,
          (i) => Container(
            width: 28,
            height: 5,
            margin: const EdgeInsets.only(right: 4),
            decoration: BoxDecoration(
              color: i < losses ? color : _dividerColor,
              borderRadius: BorderRadius.circular(3),
            ),
          ),
        ),
        const SizedBox(width: 10),
        Text(
          '$losses/3 losses',
          style: _satoshi(
            color: color,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ],
    );
  }

  Widget _buildStat(String label, String value, Color valueColor) => Expanded(
    child: Column(
      children: [
        Text(
          label,
          style: _satoshi(
            color: _textMuted,
            fontSize: 11,
            fontWeight: FontWeight.w500,
          ),
        ),
        const SizedBox(height: 4),
        FittedBox(
          fit: BoxFit.scaleDown,
          child: Text(
            value,
            style: _satoshi(
              color: valueColor,
              fontSize: 15,
              fontWeight: FontWeight.w900,
            ),
          ),
        ),
      ],
    ),
  );

  Widget _vDivider() => Container(
    height: 30,
    width: 1.5,
    color: _dividerColor,
    margin: const EdgeInsets.symmetric(horizontal: 4),
  );

  Widget _buildAction(
    IconData icon,
    String label,
    VoidCallback onTap,
    bool primary,
  ) => GestureDetector(
    onTap: onTap,
    child: Column(
      children: [
        Container(
          height: 54,
          width: 54,
          decoration: BoxDecoration(
            color: primary ? _primaryGreen : _bgColor,
            borderRadius: BorderRadius.circular(100),
            border: primary
                ? null
                : Border.all(color: _dividerColor, width: 1.5),
          ),
          child: Icon(
            icon,
            color: primary ? Colors.white : _textDark,
            size: 24,
          ),
        ),
        const SizedBox(height: 10),
        Text(
          label,
          style: _satoshi(
            color: _textDark,
            fontSize: 12,
            fontWeight: FontWeight.w700,
          ),
        ),
      ],
    ),
  );

  Widget _buildScanRing(double offset, double maxSize) =>
      AnimatedBuilder(
        animation: _scanController,
        builder: (_, child) {
          final v = (_scanController.value + offset) % 1.0;
          final size = maxSize * 0.28 + maxSize * 0.72 * v;
          final opacity = ((1.0 - v) * 0.55).clamp(0.0, 1.0);
          return SizedBox(
            width: maxSize,
            height: maxSize,
            child: Center(
              child: Opacity(
                opacity: opacity,
                child: Container(
                  width: size,
                  height: size,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(color: _primaryGreen, width: 1.2),
                  ),
                ),
              ),
            ),
          );
        },
      );

  Widget _buildEmptyTrades(bool tradeInProgress, String marketBias) {
    if (tradeInProgress) {
      final bool isBull = marketBias.contains('BULL') || marketBias.contains('CALL');
      final bool isBear = marketBias.contains('BEAR') || marketBias.contains('PUT');
      final Color dirColor = isBull ? _primaryGreen : isBear ? _dangerRed : _amber;
      final String dirLabel = isBull ? 'CALL ↑' : isBear ? 'PUT ↓' : 'ANALYZING';
      final IconData dirIcon = isBull
          ? Icons.trending_up_rounded
          : isBear
          ? Icons.trending_down_rounded
          : Icons.bolt_rounded;

      return Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(20),
        decoration: BoxDecoration(
          color: dirColor.withValues(alpha: 0.04),
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: dirColor.withValues(alpha: 0.15)),
        ),
        child: Row(
          children: [
            AnimatedBuilder(
              animation: _scanController,
              builder: (_, _) {
                final pulse = 0.9 + 0.1 * (1 + sin(_scanController.value * 2 * pi)) / 2;
                return Transform.scale(
                  scale: pulse,
                  child: Container(
                    width: 44,
                    height: 44,
                    decoration: BoxDecoration(
                      color: dirColor.withValues(alpha: 0.12),
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(color: dirColor.withValues(alpha: 0.3)),
                    ),
                    child: Icon(dirIcon, color: dirColor, size: 22),
                  ),
                );
              },
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Text(
                        dirLabel,
                        style: _satoshi(color: dirColor, fontSize: 13, fontWeight: FontWeight.w900, letterSpacing: 0.3),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        'XAU/USD',
                        style: _satoshi(color: _textMuted, fontSize: 12, fontWeight: FontWeight.w600),
                      ),
                    ],
                  ),
                  const SizedBox(height: 3),
                  Text(
                    '15 min contract · Awaiting settlement',
                    style: _satoshi(color: _textMuted, fontSize: 12, fontWeight: FontWeight.w500),
                  ),
                ],
              ),
            ),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
              decoration: BoxDecoration(
                color: _primaryGreen.withValues(alpha: 0.1),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Container(width: 6, height: 6, decoration: BoxDecoration(color: _primaryGreen, shape: BoxShape.circle)),
                  const SizedBox(width: 5),
                  Text('LIVE', style: _satoshi(color: _primaryGreen, fontSize: 10, fontWeight: FontWeight.w900, letterSpacing: 0.5)),
                ],
              ),
            ),
          ],
        ),
      );
    }

    // Animated radar scanner for XAU/USD signal search
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(vertical: 40),
      child: Column(
        children: [
          SizedBox(
            width: 110,
            height: 110,
            child: Stack(
              alignment: Alignment.center,
              children: [
                _buildScanRing(0.0, 110),
                _buildScanRing(0.33, 110),
                _buildScanRing(0.66, 110),
                Container(
                  width: 48,
                  height: 48,
                  decoration: BoxDecoration(
                    color: _primaryGreen.withValues(alpha: 0.08),
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: _primaryGreen.withValues(alpha: 0.35),
                      width: 1.5,
                    ),
                  ),
                  child: Icon(
                    Icons.candlestick_chart_outlined,
                    color: _primaryGreen,
                    size: 22,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 20),
          Text(
            'Scanning XAU/USD',
            style: _satoshi(
              color: _textDark,
              fontSize: 15,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            'Waiting for confluence signal...',
            style: _satoshi(
              color: _textMuted,
              fontSize: 13,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}
