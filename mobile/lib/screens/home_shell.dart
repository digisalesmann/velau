import 'package:flutter/material.dart';

// Import the screens that will live inside the tabs
import 'dashboard_screen.dart';
import 'signals_screen.dart';
import 'trade_history_screen.dart';
import 'account_screen.dart';

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  /// Set this from anywhere to switch the active tab without pushing a route.
  static final tabNotifier = ValueNotifier<int>(0);

  /// Increment this after a Deriv connect/disconnect to trigger a refresh on
  /// all screens that depend on the connection state.
  static final derivNotifier = ValueNotifier<int>(0);

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _currentIndex = 0;

  @override
  void initState() {
    super.initState();
    HomeShell.tabNotifier.addListener(_onTabChange);
  }

  @override
  void dispose() {
    HomeShell.tabNotifier.removeListener(_onTabChange);
    super.dispose();
  }

  void _onTabChange() {
    setState(() => _currentIndex = HomeShell.tabNotifier.value);
  }

  // The screens stacked in the background.
  // IndexedStack keeps them alive so they don't reload when switching tabs.
  final List<Widget> _screens = [
    const DashboardScreen(),
    const SignalsScreen(), // Placeholder until we build it
    const TradeHistoryScreen(),
    const AccountScreen(), // Placeholder until we build it
  ];

  // Core Palette
  final Color _bgColor = const Color(0xFFFFFFFF);
  final Color _primaryGreen = const Color(0xFF10B981);
  final Color _unselectedIcon = const Color(0xFF94A3B8); // Cool muted slate
  final Color _borderColor = const Color(0xFFF1F5F9);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bgColor,
      body: IndexedStack(index: _currentIndex, children: _screens),
      // Custom flat bottom nav to maintain the "cardless" prosthetic feel
      bottomNavigationBar: Container(
        decoration: BoxDecoration(
          color: _bgColor,
          border: Border(top: BorderSide(color: _borderColor, width: 1.5)),
        ),
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.only(top: 12.0, bottom: 8.0),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                _buildNavItem(Icons.dashboard_rounded, 'Home', 0),
                _buildNavItem(Icons.insights_rounded, 'Signals', 1),
                _buildNavItem(Icons.history_rounded, 'History', 2),
                _buildNavItem(Icons.person_outline_rounded, 'Account', 3),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildNavItem(IconData icon, String label, int index) {
    final isSelected = _currentIndex == index;

    return GestureDetector(
      onTap: () => HomeShell.tabNotifier.value = index,
      behavior: HitTestBehavior.opaque, // Ensures the whole area is tappable
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
        width: 72, // Fixed width prevents shifting when font weight changes
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              icon,
              color: isSelected ? _primaryGreen : _unselectedIcon,
              size: 26,
            ),
            const SizedBox(height: 6),
            Text(
              label,
              style: TextStyle(
                fontFamily: 'Satoshi',
                color: isSelected ? _primaryGreen : _unselectedIcon,
                fontSize: 11,
                fontWeight: isSelected ? FontWeight.w900 : FontWeight.w500,
                letterSpacing: 0.2,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
