using System.Globalization;
using System.Windows.Data;

namespace Launcher.Converters;

/// <summary>
/// 取字符串首字符大写，用作图标占位
/// </summary>
public class FirstLetterConverter : IValueConverter
{
    public static readonly FirstLetterConverter Instance = new();

    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        if (value is string s && !string.IsNullOrEmpty(s))
            return s[..1].ToUpper();
        return "?";
    }

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotImplementedException();
}
