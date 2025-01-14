import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from typing import Dict, Optional
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import logging
from data_handler import DukasCopyDataHandler
from strategy import TradingStrategy

class TrendRegime(Enum):
    UPTREND = 'uptrend'
    DOWNTREND = 'downtrend'
    SIDEWAYS = 'sideways'
    UNCLASSIFIED = 'unclassified'  # Added for trades without regime data due to 200 MA

class VolatilityRegime(Enum):
    HIGH = 'high'
    MEDIUM = 'medium'
    LOW = 'low'

@dataclass
class RegimeMetrics:
    """Performance metrics for a specific market regime"""
    n_trades: int
    win_rate: float
    avg_pnl: float
    total_pnl: float
    sharpe: Optional[float] = None

class MarketAnalysis:
    def __init__(self, data: pd.DataFrame, initial_capital: float = 100000):
        self.data = data
        self.daily_data = None
        self.regimes = None
        self.initial_capital = initial_capital
        
        # Parameters for regime classification
        self.short_ma = 20
        self.long_ma = 200
        self.vol_window = 21
        
        self._prepare_daily_data()

    def _calculate_regime_sharpe(self, regime_trades: pd.DataFrame) -> Optional[float]:
        """
        Calculate Sharpe ratio for a specific market regime using percentage returns
        
        Parameters:
        -----------
        regime_trades : pd.DataFrame
            DataFrame containing trades for a specific regime
            
        Returns:
        --------
        float or None
            Sharpe ratio for the regime or None if insufficient data
        """
        if len(regime_trades) < 20:  # Minimum trades requirement
            return None
            
        # Create a copy to avoid SettingWithCopyWarning
        regime_data = regime_trades.copy()
        
        # Calculate percentage returns
        regime_data.loc[:, 'return_pct'] = regime_data['pnl'] / self.initial_capital * 100
            
        # Calculate daily returns
        daily_returns = regime_data.groupby(
            pd.to_datetime(regime_data['entry_time']).dt.date
        )['return_pct'].sum()
        
        if len(daily_returns) < 20 or daily_returns.std() == 0:  # Minimum days requirement
            return None
            
        # Calculate number of trading days in regime
        trading_days = daily_returns.index.nunique()
        if trading_days < 20:
            return None
        
        # Annualize using actual trading days in regime
        annualized_return = daily_returns.mean() * trading_days
        annualized_vol = daily_returns.std() * np.sqrt(trading_days)
        
        return annualized_return / annualized_vol
        
    def _prepare_daily_data(self):
        """Prepare daily data with technical indicators using warmup"""
        if 'date' in self.data.columns:
            self.daily_data = pd.DataFrame({
                'open': self.data.groupby('date')['open'].first(),
                'high': self.data.groupby('date')['high'].max(),
                'low': self.data.groupby('date')['low'].min(),
                'close': self.data.groupby('date')['close'].last(),
                'volume': self.data.groupby('date')['volume'].sum()
            })
            
            # Use ffill() instead of fillna(method='ffill')
            self.daily_data = self.daily_data.ffill()
            
            # Calculate indicators with min_periods=1 to start earlier
            self.daily_data['ma_short'] = self.daily_data['close'].ewm(span=self.short_ma, min_periods=1).mean()
            self.daily_data['ma_long'] = self.daily_data['close'].ewm(span=self.long_ma, min_periods=1).mean()
            
            # ATR calculation with minimum periods
            tr = pd.DataFrame({
                'h-l': self.daily_data['high'] - self.daily_data['low'],
                'h-pc': abs(self.daily_data['high'] - self.daily_data['close'].shift(1)),
                'l-pc': abs(self.daily_data['low'] - self.daily_data['close'].shift(1))
            }).fillna(0)  # This fillna is fine as it's using a value, not a method
            
            self.daily_data['atr'] = tr.max(axis=1).ewm(span=self.vol_window, min_periods=1).mean()
        else:
            self.daily_data = self.data.copy()
        
        # Remove any remaining NaN values
        self.daily_data = self.daily_data.dropna()
    
    def classify_regimes(self) -> pd.DataFrame:
        """Classify market regimes with improved continuity"""
        if self.daily_data is None:
            self._prepare_daily_data()
            
        conditions = pd.DataFrame({
            'above_long_ma': self.daily_data['close'] > self.daily_data['ma_long'],
            'rising_short_ma': self.daily_data['ma_short'] > self.daily_data['ma_short'].shift(1)
        }).fillna(False)  # This fillna is fine as it's using a value, not a method
        
        trends = pd.Series(index=self.daily_data.index, dtype='object')
        trends[conditions.all(axis=1)] = TrendRegime.UPTREND.value
        trends[~conditions.any(axis=1)] = TrendRegime.DOWNTREND.value
        trends[trends.isna()] = TrendRegime.SIDEWAYS.value
        
        vol_quantiles = self.daily_data['atr'].quantile([0.33, 0.67])
        volatility = pd.Series(index=self.daily_data.index, dtype='object')
        volatility[self.daily_data['atr'] <= vol_quantiles[0.33]] = VolatilityRegime.LOW.value
        volatility[self.daily_data['atr'] >= vol_quantiles[0.67]] = VolatilityRegime.HIGH.value
        volatility[volatility.isna()] = VolatilityRegime.MEDIUM.value
        
        self.regimes = pd.DataFrame({
            'trend': trends,
            'volatility': volatility,
            'atr': self.daily_data['atr']
        })
        
        return self.regimes
    
    def analyze_performance(self, trades_df: pd.DataFrame) -> Dict[str, RegimeMetrics]:
        """Analyze strategy performance in different market regimes"""
        if self.regimes is None:
            self.classify_regimes()
            
        # Create a copy of trades DataFrame and add percentage returns
        trades_analysis = trades_df.copy()
        trades_analysis.loc[:, 'return_pct'] = trades_analysis['pnl'] / self.initial_capital * 100
            
        # Merge trades with regimes, keeping all trades
        trades_with_regimes = trades_analysis.merge(
            self.regimes,
            left_on=pd.to_datetime(trades_analysis['entry_time']).dt.date,
            right_index=True,
            how='left'
        )
        
        # Handle unclassified trades using loc
        trades_with_regimes.loc[trades_with_regimes['trend'].isna(), 'trend'] = TrendRegime.UNCLASSIFIED.value
        trades_with_regimes.loc[trades_with_regimes['volatility'].isna(), 'volatility'] = 'unclassified'
        
        results = {}
        
        # Calculate metrics for each trend regime
        for trend in TrendRegime:
            trend_data = trades_with_regimes.loc[trades_with_regimes['trend'] == trend.value].copy()
            if len(trend_data) > 0:
                results[f'{trend.value}_regime'] = RegimeMetrics(
                    n_trades=len(trend_data),
                    win_rate=len(trend_data[trend_data['pnl'] > 0]) / len(trend_data),
                    avg_pnl=trend_data['pnl'].mean(),
                    total_pnl=trend_data['pnl'].sum(),
                    sharpe=self._calculate_regime_sharpe(trend_data)
                )
        
        # Calculate metrics for each volatility regime
        for vol in VolatilityRegime:
            vol_data = trades_with_regimes.loc[trades_with_regimes['volatility'] == vol.value].copy()
            if len(vol_data) > 0:
                results[f'{vol.value}_vol'] = RegimeMetrics(
                    n_trades=len(vol_data),
                    win_rate=len(vol_data[vol_data['pnl'] > 0]) / len(vol_data),
                    avg_pnl=vol_data['pnl'].mean(),
                    total_pnl=vol_data['pnl'].sum(),
                    sharpe=self._calculate_regime_sharpe(vol_data)
                )
        
        return results, trades_with_regimes

    def plot_analysis(self, trades_df: pd.DataFrame, save_path: Optional[str] = None) -> None:
        """Create visualization of regime analysis"""
        if self.regimes is None:
            self.classify_regimes()
                
        regime_results, _ = self.analyze_performance(trades_df)  # Unpack only the metrics
            
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 12))
            
        # 1. Price with Moving Averages
        ax1.plot(self.daily_data.index, self.daily_data['close'], label='Price')
        ax1.plot(self.daily_data.index, self.daily_data['ma_short'], 
                label=f'{self.short_ma}MA', alpha=0.7)
        ax1.plot(self.daily_data.index, self.daily_data['ma_long'], 
                label=f'{self.long_ma}MA', alpha=0.7)
        ax1.set_title('Price and Moving Averages')
        ax1.legend()
        ax1.grid(True)
            
        # 2. ATR Evolution
        ax2.plot(self.daily_data.index, self.daily_data['atr'])
        percentiles = self.daily_data['atr'].quantile([0.33, 0.67])
        for percentile in percentiles:
            ax2.axhline(y=percentile, color='r', linestyle='--', alpha=0.5)
        ax2.set_title('Average True Range (ATR)')
        ax2.grid(True)
            
        # 3. Performance by Trend Regime
        trend_metrics = [m for k, m in regime_results.items() if 'regime' in k]
        trend_labels = [k.replace('_regime', '') for k in regime_results.keys() if 'regime' in k]
        ax3.bar(trend_labels, [m.total_pnl for m in trend_metrics])
        ax3.set_title('P&L by Trend Regime')
            
        for i, metrics in enumerate(trend_metrics):
            ax3.annotate(f'n={metrics.n_trades}\nWR={metrics.win_rate:.1%}',
                        xy=(i, metrics.total_pnl),
                        xytext=(0, 10 if metrics.total_pnl >= 0 else -10),
                        textcoords='offset points',
                        ha='center',
                        va='bottom' if metrics.total_pnl >= 0 else 'top')
        ax3.grid(True)
            
        # 4. Performance by Volatility Regime
        vol_metrics = [m for k, m in regime_results.items() if '_vol' in k]
        vol_labels = [k.replace('_vol', '') for k in regime_results.keys() if '_vol' in k]
        ax4.bar(vol_labels, [m.total_pnl for m in vol_metrics])
        ax4.set_title('P&L by Volatility Regime')
            
        for i, metrics in enumerate(vol_metrics):
            ax4.annotate(f'n={metrics.n_trades}\nWR={metrics.win_rate:.1%}',
                        xy=(i, metrics.total_pnl),
                        xytext=(0, 10 if metrics.total_pnl >= 0 else -10),
                        textcoords='offset points',
                        ha='center',
                        va='bottom' if metrics.total_pnl >= 0 else 'top')
        ax4.grid(True)
            
        plt.tight_layout()
            
        if save_path:
            plt.savefig(save_path)
            plt.close()
        else:
            plt.show()

def format_regime_metrics(name: str, metrics: RegimeMetrics) -> str:
        """Format regime metrics with clearer Sharpe ratio display"""
        sharpe_str = ""
        if metrics.sharpe is not None:
            sharpe_str = f"    Sharpe Ratio: {metrics.sharpe:.2f}"
            if metrics.n_trades < 30:
                sharpe_str += " (limited data)"
        else:
            sharpe_str = "    Sharpe Ratio: Insufficient data"
        
        return f"""
    {name}:
        Number of Trades: {metrics.n_trades}
        Win Rate: {metrics.win_rate:.2%}
        Average P&L: ${metrics.avg_pnl:.2f}
        Total P&L: ${metrics.total_pnl:.2f}
    {sharpe_str}"""

def main():
    # Configure logging with a cleaner format
    logging.basicConfig(
        level=logging.INFO,
        format='%(message)s'
    )
    logger = logging.getLogger(__name__)

    try:
        # Load data
        data_path = Path("datasets/raw/XAUUSD/Nov23-Nov24/XAUUSD_1M_BID.csv")
        handler = DukasCopyDataHandler()
        data = handler.load_data(data_path)
        
        # Run strategy
        logger.info("Running strategy...")
        strategy = TradingStrategy(data)
        strategy.generate_signals()
        strategy.simulate_trades()
        trades_df = strategy.get_trade_data()
        
        # Run market analysis
        logger.info("Analyzing market regimes...")
        market_analyzer = MarketAnalysis(data)
        market_analyzer.classify_regimes()
        
        # Get performance metrics and trades with regime data
        regime_metrics, trades_with_regimes = market_analyzer.analyze_performance(trades_df)
        
        # Print regime methodology explanation
        print("\n=== Regime Classification Methodology ===")
        print("Trend Regimes are classified using two moving averages:")
        print(f"- Short-term EMA: {market_analyzer.short_ma} periods")
        print(f"- Long-term EMA: {market_analyzer.long_ma} periods")
        print("UPTREND: Price above long MA and short MA rising")
        print("DOWNTREND: Price below long MA and short MA falling")
        print("SIDEWAYS: Mixed conditions - either price is above long MA but short MA falling,")
        print("          or price is below long MA but short MA rising")
        
        print("\nVolatility Regimes are based on ATR (Average True Range):")
        print(f"- ATR Period: {market_analyzer.vol_window}")
        print("HIGH: Top 33% of ATR values")
        print("MEDIUM: Middle 33% of ATR values")
        print("LOW: Bottom 33% of ATR values")
        
        # Print regime classification summary
        print("\n=== Regime Classification Summary ===")
        print(f"Total trades: {len(trades_df)}")
        for regime in TrendRegime:
            regime_trades = trades_with_regimes[trades_with_regimes['trend'] == regime.value]
            if len(regime_trades) > 0:
                print(f"{regime.value}: {len(regime_trades)} trades")
        
        # Print detailed performance analysis
        print("\n=== Market Regime Analysis ===")
        
        # Print trend regimes
        print("\nTrend Regime Performance:")
        trend_regimes = {k: v for k, v in regime_metrics.items() if 'regime' in k}
        for regime_name, metrics in sorted(trend_regimes.items()):
            print(format_regime_metrics(
                regime_name.replace('_regime', '').upper(),
                metrics
            ))
        
        # Print volatility regimes
        print("\nVolatility Regime Performance:")
        vol_regimes = {k: v for k, v in regime_metrics.items() if '_vol' in k}
        for regime_name, metrics in sorted(vol_regimes.items()):
            print(format_regime_metrics(
                regime_name.replace('_vol', '').upper(),
                metrics
            ))
        
        # Generate plots
        plot_path = data_path.parent.parent / 'processed' / data_path.parent.name / 'regime_analysis.png'
        plot_path.parent.mkdir(parents=True, exist_ok=True)
        market_analyzer.plot_analysis(trades_df, str(plot_path))
        logger.info(f"Analysis completed! Check {plot_path} for visualizations")
        
    except Exception as e:
        logger.error(f"Error during market analysis: {str(e)}", exc_info=True)
        raise

if __name__ == '__main__':
    main()