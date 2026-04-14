import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:intl/intl.dart';
import '../services/api_service.dart';

class TradeHistoryScreen extends StatefulWidget {
  const TradeHistoryScreen({super.key});

  @override
  State<TradeHistoryScreen> createState() => _TradeHistoryScreenState();
}

class _TradeHistoryScreenState extends State<TradeHistoryScreen> {
  final ApiService _apiService = ApiService();

  final Color _bgColor = const Color(0xFFFFFFFF);
  final Color _primaryGreen = const Color(0xFF10B981);
  final Color _textDark = const Color(0xFF111827);
  final Color _textMuted = const Color(0xFF6B7280);
  final Color _dividerColor = const Color(0xFFF3F4F6);
  final Color _lossRed = const Color(0xFFEF4444);
  final Color _separator = const Color(0xFFF1F5F9);

  List<dynamic> _trades = [];
  bool _isLoading = true;
  String? _error;

  // Stats computed from trades
  int _totalTrades = 0;
  int _wins = 0;
  double _totalPnl = 0.0;

  @override
  void initState() {
    super.initState();
    _fetchHistory();
  }

  Future<void> _fetchHistory() async {
    if (mounted) setState(() => _isLoading = true);
    try {
      final prefs = await SharedPreferences.getInstance();
      final token = prefs.getString('jwt_token');
      if (token == null) throw Exception('Authentication missing.');
      final data = await _apiService.getTradeHistory(token);
      final list = (data['history'] ?? []) as List<dynamic>;

      // Compute summary stats
      int wins = 0;
      double pnl = 0.0;
      for (final t in list) {
        final p = double.tryParse(t['pnl']?.toString() ?? '0') ?? 0.0;
        pnl += p;
        if (p > 0) wins++;
      }

      if (mounted) {
        setState(() {
          _trades = list;
          _totalTrades = list.length;
          _wins = wins;
          _totalPnl = pnl;
          _error = null;
          _isLoading = false;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = ApiService.friendlyError(e);
          _isLoading = false;
        });
      }
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

  String _formatPnl(double amount) =>
      NumberFormat('#,##0.00', 'en_US').format(amount.abs());

  String _formatTime(dynamic epoch) {
    try {
      final ts = int.tryParse(epoch?.toString() ?? '0') ?? 0;
      if (ts == 0) return '--';
      final dt = DateTime.fromMillisecondsSinceEpoch(ts * 1000);
      return DateFormat('MMM dd, HH:mm').format(dt);
    } catch (_) {
      return '--';
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bgColor,
      appBar: AppBar(
        backgroundColor: _bgColor,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleSpacing: 24,
        title: Text(
          'Trade History',
          style: _satoshi(
            color: _textDark,
            fontSize: 24,
            fontWeight: FontWeight.w900,
            letterSpacing: -0.5,
          ),
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh_rounded),
            color: _textDark,
            onPressed: _fetchHistory,
          ),
          const SizedBox(width: 8),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(1),
          child: Divider(color: _separator, height: 1, thickness: 1),
        ),
      ),
      body: SafeArea(
        child: _isLoading
            ? Center(child: CircularProgressIndicator(color: _primaryGreen))
            : _error == 'no_deriv_account'
            ? _buildConnectPrompt()
            : _error != null
            ? _buildError()
            : _trades.isEmpty
            ? _buildEmpty()
            : _buildContent(),
      ),
    );
  }

  Widget _buildContent() {
    return RefreshIndicator(
      color: _primaryGreen,
      backgroundColor: _bgColor,
      onRefresh: _fetchHistory,
      child: ListView(
        padding: const EdgeInsets.only(bottom: 40),
        children: [
          // ── Summary strip ──────────────────────────────────────────────────
          _buildSummary(),
          Divider(color: _separator, height: 1, thickness: 1),

          // ── Trade list ─────────────────────────────────────────────────────
          ...List.generate(_trades.length, (i) {
            final trade = _trades[i];
            final double pnl =
                double.tryParse(trade['pnl']?.toString() ?? '0') ?? 0.0;
            final bool win = pnl > 0;
            final String sym = trade['symbol'] ?? 'Gold Spot/U.S. Dollar';
            final String dir = trade['type'] ?? 'Options';
            final String time = _formatTime(trade['time']);
            final String cost =
                '\$${_formatPnl(double.tryParse(trade['buy_cost']?.toString() ?? '10') ?? 10.0)}';

            return Column(
              children: [
                _buildTradeItem(
                  symbol: sym,
                  type: dir,
                  cost: cost,
                  pnl: pnl,
                  time: time,
                  won: win,
                  tradeData: Map<String, dynamic>.from(trade),
                ),
                if (i < _trades.length - 1)
                  Divider(
                    color: _dividerColor,
                    height: 1,
                    thickness: 1,
                    indent: 24,
                    endIndent: 24,
                  ),
              ],
            );
          }),
        ],
      ),
    );
  }

  Widget _buildSummary() {
    final winRate = _totalTrades > 0
        ? (_wins / _totalTrades * 100).toStringAsFixed(0)
        : '0';
    final pnlColor = _totalPnl >= 0 ? _primaryGreen : _lossRed;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 20),
      child: Row(
        children: [
          _summaryCell('Trades', '$_totalTrades'),
          _vline(),
          _summaryCell('Win Rate', '$winRate%'),
          _vline(),
          _summaryCell(
            'Total P&L',
            '${_totalPnl >= 0 ? '+' : '-'}\$${_formatPnl(_totalPnl)}',
            valueColor: pnlColor,
          ),
        ],
      ),
    );
  }

  Widget _summaryCell(String label, String value, {Color? valueColor}) =>
      Expanded(
        child: Column(
          children: [
            Text(
              label,
              style: _satoshi(
                color: _textMuted,
                fontSize: 11,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              value,
              style: _satoshi(
                color: valueColor ?? _textDark,
                fontSize: 16,
                fontWeight: FontWeight.w900,
              ),
            ),
          ],
        ),
      );

  Widget _vline() => Container(
    height: 32,
    width: 1,
    margin: const EdgeInsets.symmetric(horizontal: 16),
    color: _separator,
  );

  Widget _buildTradeItem({
    required String symbol,
    required String type,
    required String cost,
    required double pnl,
    required String time,
    required bool won,
    required Map<String, dynamic> tradeData,
  }) {
    final Color color = won ? _primaryGreen : _lossRed;
    final String pnlStr = '${pnl >= 0 ? '+' : '-'}\$${_formatPnl(pnl)}';

    return InkWell(
      onTap: () => Navigator.pushNamed(
        context,
        '/trade_details',
        arguments: {
          'pair':         symbol,
          'type':         type,
          'pnl':          pnl,
          'isClosed':     true,
          'margin':       double.tryParse(tradeData['buy_cost']?.toString() ?? '10') ?? 10.0,
          'time':         tradeData['time'],
          'account_type': tradeData['account_type'] ?? '',
        },
      ),
      splashColor: _dividerColor,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
        child: Row(
          children: [
            // Details
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Flexible(
                        child: Text(
                          symbol,
                          overflow: TextOverflow.ellipsis,
                          style: _satoshi(
                            color: _textDark,
                            fontSize: 15,
                            fontWeight: FontWeight.w900,
                            letterSpacing: -0.3,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        type.contains('CALL') ? 'CALL' : 'PUT',
                        style: _satoshi(
                          color: color,
                          fontSize: 10,
                          fontWeight: FontWeight.w900,
                          letterSpacing: 0.4,
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        won ? 'WON' : 'LOST',
                        style: _satoshi(
                          color: won ? _primaryGreen : _lossRed,
                          fontSize: 10,
                          fontWeight: FontWeight.w900,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    'Stake $cost  •  $time',
                    style: _satoshi(
                      color: _textMuted,
                      fontSize: 12,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ],
              ),
            ),

            // P&L
            Text(
              pnlStr,
              style: _satoshi(
                color: color,
                fontSize: 16,
                fontWeight: FontWeight.w900,
                letterSpacing: -0.5,
              ),
            ),
          ],
        ),
      ),
    );
  }

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
            'Link your Deriv account to view\nyour trade history.',
            textAlign: TextAlign.center,
            style: _satoshi(
              color: _textMuted,
              fontSize: 14,
              fontWeight: FontWeight.w500,
            ),
          ),
          const SizedBox(height: 28),
          ElevatedButton(
            onPressed: () => Navigator.of(context).pushNamed('/account'),
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

  Widget _buildEmpty() => Center(
    child: Column(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Icon(Icons.history_rounded, color: _dividerColor, size: 64),
        const SizedBox(height: 16),
        Text(
          'No completed trades yet.',
          style: _satoshi(
            color: _textMuted,
            fontSize: 16,
            fontWeight: FontWeight.w500,
          ),
        ),
        const SizedBox(height: 8),
        Text(
          'Trades appear here once the bot executes and settles.',
          textAlign: TextAlign.center,
          style: _satoshi(
            color: _textMuted.withValues(alpha: 0.6),
            fontSize: 13,
            fontWeight: FontWeight.w400,
          ),
        ),
      ],
    ),
  );

  Widget _buildError() => Center(
    child: Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(Icons.error_outline_rounded, color: _lossRed, size: 48),
          const SizedBox(height: 16),
          Text(
            'Failed to load history',
            style: _satoshi(
              color: _textDark,
              fontSize: 18,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            _error!,
            textAlign: TextAlign.center,
            style: _satoshi(
              color: _textMuted,
              fontSize: 14,
              fontWeight: FontWeight.w500,
            ),
          ),
          const SizedBox(height: 24),
          ElevatedButton(
            onPressed: _fetchHistory,
            style: ElevatedButton.styleFrom(
              backgroundColor: _textDark,
              elevation: 0,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(100),
              ),
            ),
            child: Text(
              'Retry',
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
}
