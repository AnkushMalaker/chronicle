import React from 'react';
import { View, StyleSheet } from 'react-native';

interface StatusIndicatorProps {
  isActive: boolean;
  size?: number;
  activeColor?: string;
  inactiveColor?: string;
}

const StatusIndicator: React.FC<StatusIndicatorProps> = ({
  isActive,
  size = 10,
  activeColor = '#4CD964', // Green
  inactiveColor = '#FF3B30', // Red
}) => {
  return (
    <View 
      style={[
        styles.dot,
        { 
          width: size, 
          height: size, 
          borderRadius: size / 2,
          backgroundColor: isActive ? activeColor : inactiveColor,
        }
      ]}
    />
  );
};

const styles = StyleSheet.create({
  dot: {
    marginHorizontal: 8, // Add some spacing around the dot
    // elevation: 1, // Optional: add a slight shadow for depth
    // shadowColor: '#000',
    // shadowOffset: { width: 0, height: 1 },
    // shadowOpacity: 0.2,
    // shadowRadius: 1,
  },
});

export default StatusIndicator; 